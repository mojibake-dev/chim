"""The roller: freeze an OpenMW/TES3MP ``Data Files`` into a portable lockfile + bundle.

One ``modlock.json`` is the seam of the multiplayer pipeline (see the pipeline plan and
``docs/modding``): chim *emits* it here on the Mac, sibling **baar-dau** *applies* it on the
server (writing ``requiredDataFiles.json`` + restarting), and friends get a self-contained
zip. Everything downstream converges on the same bytes, so TES3MP -- which enforces the
modlist by plugin **name + load order + CRC32** -- sees an identical loadout on the server
and every client.

What a *roll* produces from an ``openmw.cfg`` (its four axes read via :mod:`chim.loadorder`):

* **``modlock.json``** -- the portable manifest: engine pin, the ordered ``content`` plugins
  each with sha256+crc32+size+masters, the ``fallback-archive`` / ``groundcover`` axes, and a
  complete ``files`` list (every bundled file, hashed) for byte-identity verification.
* **``requiredDataFiles.json``** -- derived: the TES3MP array-of-``{name:[crc32]}`` the server
  enforces (order = load order; empty checksum list = name+order only).
* **the canon zip** -- the bundled data dir(s), laid out as a client drops into its Data Files.
* **``INSTALL.md``** -- generated per-OS wipe-and-unzip steps + the load order + the zip hash.

Two kinds of data dir: the ones we **bundle** (the canon mods, shipped) and the user's
**vanilla** Morrowind install (its plugins are still hashed into ``content``/``requiredDataFiles``
so the server enforces them, but its bytes are never redistributed). Plugins resolve across all
data dirs OpenMW-style (later dirs win); only ``bundle_dirs`` files enter the zip + ``files``.

Local-only by design: a roll hashes a whole tree, so it runs on the machine that holds the
canon (the Mac), on plain filesystem paths -- no remote host indirection.
"""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
import zlib
from typing import Dict, List, Optional, Sequence, Tuple

from . import loadorder

MODLOCK_VERSION = 1
_PLUGIN_EXTS = (".esp", ".esm", ".omwaddon", ".omwscripts")
# The incremental-update client scripts, shipped in every canon zip so a cold-installed friend
# already has the update tool. Repo-relative to this module (the operator rolls from the editable
# checkout, where chim/clients/ sits beside the package).
_CLIENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "clients")

# TES3MP accepts *any* CRC in a plugin's list, so the vanilla masters carry every known-good
# edition CRC -- an English or Russian copy must both pass, since friends supply their OWN
# vanilla ``.esm`` (we never redistribute them). Non-vanilla canon plugins get a single CRC
# because their exact bytes ship in the zip. These three pairs are the exact allowlist the live
# server prints in its "To join this server, use:" mismatch comparison (English
# first == the user's GOTY copy / ``zlib.crc32``; Russian second) -- so applying the canon must
# preserve both, or a friend on the other edition is rejected on a file the server used to take.
VANILLA_CRCS: Dict[str, List[str]] = {
    "morrowind.esm": ["0x7B6AF5B9", "0x34282D67"],
    "tribunal.esm": ["0xF481F334", "0x211329EF"],
    "bloodmoon.esm": ["0x43DD2132", "0x9EB62F26"],
}


class ModlockError(Exception):
    """A roll could not be produced (missing plugin, unsupported Lua on 0.47, ...)."""


# --------------------------------------------------------------------------- #
# hashing
# --------------------------------------------------------------------------- #

def hash_file(path: str) -> Tuple[str, int, int]:
    """Return ``(sha256_hex, crc32, size)`` for a file, streamed. crc32 is the standard
    (zlib / IEEE 802.3) checksum -- the one TES3MP's ``requiredDataFiles.json`` uses."""
    h = hashlib.sha256()
    crc = 0
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            crc = zlib.crc32(chunk, crc)
            size += len(chunk)
    return h.hexdigest(), crc & 0xFFFFFFFF, size


def crc_hex(crc: int) -> str:
    """CRC32 int -> the ``0x``+uppercase-8-digit string TES3MP tooling emits."""
    return "0x%08X" % (crc & 0xFFFFFFFF)


def _resolve_across(name: str, data_dirs: Sequence[str]) -> Optional[str]:
    """Find ``name`` in the data dirs the OpenMW way: later dirs override earlier ones."""
    for d in reversed(list(data_dirs)):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def _walk_files(root: str):
    """Yield ``(relpath_posix, abspath)`` for every file under ``root`` (sorted, stable)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for f in sorted(filenames):
            ap = os.path.join(dirpath, f)
            rel = os.path.relpath(ap, root).replace(os.sep, "/")
            yield rel, ap


# --------------------------------------------------------------------------- #
# build the modlock
# --------------------------------------------------------------------------- #

def build_modlock(cfg_path: str, *, bundle_dirs: Sequence[str],
                  data_dirs: Optional[Sequence[str]] = None,
                  engine: Optional[Dict] = None,
                  allow_lua: Optional[bool] = None) -> Dict:
    """Freeze an ``openmw.cfg`` into the portable modlock dict (no timestamp inside, so the
    same inputs freeze byte-identically; stamp the roll outside if you want a date).

    ``bundle_dirs`` are the data dirs whose files ship in the zip (the canon mods).
    ``data_dirs`` (default: the cfg's ``data=`` list) is the full search path used to resolve
    + hash each content plugin, including the user's vanilla Morrowind install. ``engine`` pins
    the target (``{fork, openmw_base, protocol}``). ``allow_lua`` gates ``.omwscripts``; when
    None it is inferred from the engine (TES3MP's ~0.47 base has no OpenMW-Lua, so a
    ``.omwscripts`` in the load order is a hard error -- catch it at the roll, not in-game).
    """
    data_dirs = list(data_dirs if data_dirs is not None else loadorder.read_data_dirs(cfg_path))
    content = loadorder.read_content(cfg_path)
    archives = loadorder.read_archives(cfg_path)
    groundcover = loadorder.read_groundcover(cfg_path)
    engine = dict(engine or {"fork": "tes3mp", "openmw_base": None, "protocol": None})

    if allow_lua is None:
        allow_lua = _engine_allows_lua(engine)
    lua = [c for c in content if c.lower().endswith(".omwscripts")]
    if lua and not allow_lua:
        raise ModlockError(
            "OpenMW-Lua content is not supported on this engine "
            f"({engine.get('openmw_base') or 'pre-0.49'}); remove or replace: {lua}"
        )

    # content plugins: resolve across all data dirs (vanilla + canon), hash each.
    content_entries: List[Dict] = []
    for name in content:
        p = _resolve_across(name, data_dirs)
        if p is None:
            raise ModlockError(f"content plugin not found in any data dir: {name!r}")
        sha, crc, size = hash_file(p)
        content_entries.append({
            "name": name, "sha256": sha, "crc32": crc_hex(crc), "size": size,
            "masters": loadorder.plugin_masters(os.path.dirname(p), name),
        })

    # bundled files: everything under bundle_dirs, merged (later dir wins on path collision).
    merged: Dict[str, str] = {}
    for d in bundle_dirs:
        if not os.path.isdir(d):
            raise ModlockError(f"bundle dir does not exist: {d!r}")
        for rel, ap in _walk_files(d):
            merged[rel] = ap
    files: List[Dict] = []
    total = 0
    for rel in sorted(merged):
        sha, crc, size = hash_file(merged[rel])
        files.append({"path": rel, "sha256": sha, "crc32": crc_hex(crc), "size": size})
        total += size

    return {
        "modlock_version": MODLOCK_VERSION,
        "engine": engine,
        "content": content_entries,
        "archives": archives,
        "groundcover": groundcover,
        "files": files,
        "stats": {"plugins": len(content_entries), "file_count": len(files), "total_bytes": total},
    }


def _engine_allows_lua(engine: Dict) -> bool:
    """OpenMW-Lua (``.omwscripts``) landed in 0.48+. TES3MP's ~0.47 base has none."""
    base = engine.get("openmw_base")
    if not base:
        return False  # unknown / TES3MP default: assume no Lua, the safe stance
    try:
        parts = tuple(int(x) for x in str(base).split(".")[:2])
    except ValueError:
        return False
    return parts >= (0, 48)


# --------------------------------------------------------------------------- #
# derivations
# --------------------------------------------------------------------------- #

def required_data_files(modlock: Dict, *, enforce_checksums: Optional[bool] = None,
                        vanilla_crcs: Optional[Dict[str, List[str]]] = None
                        ) -> List[Dict[str, List[str]]]:
    """Derive TES3MP's ``requiredDataFiles.json`` body from a modlock.

    A JSON **array** of single-key objects ``{"Plugin.esp": [checksums]}`` where **array order
    is the enforced load order** and an **empty checksum list = name+order only**.

    Per-plugin CRC policy: the three vanilla masters carry every known-good edition CRC (see
    :data:`VANILLA_CRCS`) so any legit copy of a friend's own Morrowind passes; every other
    plugin gets the single CRC of the exact bytes shipped in the zip. With
    ``enforce_checksums=False`` every list is empty (name+order only). ``enforce_checksums``
    defaults to the modlock's persisted value (what the roll chose), else True -- so baar-dau
    derives an identical file from the same modlock. The modlock is the contract.
    """
    if enforce_checksums is None:
        enforce_checksums = bool(modlock.get("enforce_checksums", True))
    allow = {k.lower(): list(v) for k, v in (vanilla_crcs or VANILLA_CRCS).items()}
    out: List[Dict[str, List[str]]] = []
    for c in modlock.get("content", []):
        name = c["name"]
        if not enforce_checksums:
            out.append({name: []})
            continue
        own = [c["crc32"]] if c.get("crc32") else []
        # known-edition CRCs first, then this file's own; dedup preserving order
        checks = list(dict.fromkeys(allow.get(name.lower(), []) + own))
        out.append({name: checks})
    return out


def assert_roll_sane(modlock: Dict, *, bundle_dirs: Sequence[str], data_dirs: Sequence[str],
                     expect_plugins: Optional[int] = None, forbid_substrings: Sequence[str] = (),
                     vanilla_names: Sequence[str] = ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm")
                     ) -> bool:
    """Guardrails the adversarial review demanded -- fail the roll loudly rather than ship a
    broken canon. Raises :class:`ModlockError` on any violation:

    * the content plugin count differs from ``expect_plugins`` (if given);
    * a cut-list name (``forbid_substrings``, case-insensitive substring) is in content;
    * a content plugin resolves **outside** every ``bundle_dir`` (its bytes would be enforced
      but never shipped) -- allowed only for the ``vanilla_names`` masters friends supply.
    """
    content = [c["name"] for c in modlock.get("content", [])]
    if expect_plugins is not None and len(content) != expect_plugins:
        raise ModlockError("content has %d plugins, expected %d" % (len(content), expect_plugins))
    forbid = [s.lower() for s in forbid_substrings]
    for name in content:
        low = name.lower()
        for sub in forbid:
            if sub in low:
                raise ModlockError("cut-list plugin present in content: %r (matched %r)" % (name, sub))
    vanilla = {v.lower() for v in vanilla_names}
    bundles = [os.path.abspath(d) for d in bundle_dirs]
    for name in content:
        if name.lower() in vanilla:
            continue
        p = _resolve_across(name, data_dirs)
        if p is None:
            raise ModlockError("content plugin not found in any data dir: %r" % name)
        ap = os.path.abspath(p)
        if not any(ap == b or ap.startswith(b + os.sep) for b in bundles):
            raise ModlockError(
                "content plugin %r resolves outside the bundle (%s): its bytes would be enforced "
                "but not shipped -- move it into a bundle dir" % (name, p))
    return True


def verify_zip_crc(zip_path: str, modlock: Dict) -> List[str]:
    """Re-read each content plugin from the written zip and confirm its CRC32 equals the
    modlock's -- closes the loop that the bytes friends receive reproduce the enforced checksum.
    Returns the list of mismatched plugin names (empty = good). Plugins not in the zip (the
    vanilla masters) are skipped."""
    want = {c["name"]: c["crc32"] for c in modlock.get("content", [])}
    bad: List[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        have = set(zf.namelist())
        for name, crc in want.items():
            if name not in have:
                continue
            got = crc_hex(zlib.crc32(zf.read(name)) & 0xFFFFFFFF)
            if got != crc:
                bad.append(name)
    return bad


# Already-compressed payloads: deflate wastes CPU for ~0 gain, so STORE them. Everything else
# (meshes, plugins, .tga/.bmp, PCM .wav, text) deflates well and stays DEFLATED.
_STORED_EXTS = (".dds", ".bik", ".mp3", ".ogg", ".jpg", ".jpeg", ".png", ".zip", ".7z", ".rar", ".bsa")


def write_bundle_zip(bundle_dirs: Sequence[str], zip_path: str, *,
                     extra_files: Optional[Dict[str, str]] = None) -> Dict:
    """Zip the bundle dirs into ``zip_path`` (deterministic order, canon Data Files layout).

    Paths are relative to each bundle dir's root, merged (later dir wins), so a client unzips
    it straight into its Data Files dir. Uses a fixed timestamp per entry so the archive is
    reproducible, and STORES already-compressed payloads (see :data:`_STORED_EXTS`) so an
    all-``.dds`` texture bundle doesn't burn CPU deflating for nothing.

    ``extra_files`` maps an in-zip arcname -> UTF-8 text, written alongside the bundle -- how the
    roll ships the install docs (a ready ``openmw.cfg`` + ``INSTALL.md``) INSIDE the zip so an
    extracted canon is self-documenting. They deflate (text) and, being deterministic from the
    modlock, keep the archive reproducible. Returns ``{zip, sha256, size, file_count, extra_files}``
    (``file_count`` counts bundle files only; the docs are listed in ``extra_files``)."""
    # Merge CASE-INSENSITIVELY (later dir wins). The target Data Files is case-insensitive
    # (Windows + macOS, and OpenMW's VFS regardless of host FS), so two bundle dirs supplying
    # the same asset at case-differing paths -- e.g. Familiar Faces' `Meshes/x.nif` vs a base
    # mod's `meshes/x.NIF` -- are the SAME file. Keying on the exact-case path kept BOTH, which
    # then collide on unzip and let the earlier (wrong) one win, reintroducing cut content.
    merged: Dict[str, Tuple[str, str]] = {}  # casefold(rel) -> (rel-as-written, abspath)
    for d in bundle_dirs:
        for rel, ap in _walk_files(d):
            merged[rel.lower()] = (rel, ap)
    extra = dict(extra_files or {})
    os.makedirs(os.path.dirname(os.path.abspath(zip_path)) or ".", exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for key in sorted(merged):
            rel, ap = merged[key]
            zi = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))  # reproducible
            zi.compress_type = (zipfile.ZIP_STORED if rel.lower().endswith(_STORED_EXTS)
                                else zipfile.ZIP_DEFLATED)
            zi.external_attr = 0o644 << 16
            with open(ap, "rb") as fh:
                zf.writestr(zi, fh.read())
        for arcname in sorted(extra):
            zi = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, extra[arcname].encode("utf-8"))
    sha, _crc, size = hash_file(zip_path)
    return {"zip": zip_path, "sha256": sha, "size": size,
            "file_count": len(merged), "extra_files": sorted(extra)}


def render_install_md(modlock: Dict, *, zip_name: str, zip_sha256: Optional[str] = None,
                      server_address: Optional[str] = None, title: str = "Morrowind Multiplayer",
                      bundled_cfg: Optional[str] = None,
                      bundled_client_cfg: Optional[str] = None) -> str:
    """Generate the friend ``INSTALL.md`` from a modlock. With ``bundled_cfg`` (a ready
    ``openmw.cfg`` shipped in the zip) it's the dead-simple "place these files, edit the one noted
    bit" guide -- the roll pre-rolls the configs so a friend never hand-types the load order;
    ``bundled_client_cfg`` adds the ``tes3mp-client-default.cfg`` placement step. Without
    ``bundled_cfg`` it falls back to the older self-serve wipe/unzip + hand-listed order."""
    content = modlock.get("content", [])
    n = len(content)
    order = "\n".join(f"{i + 1}. {c['name']}" for i, c in enumerate(content))
    archives = modlock.get("archives", [])
    arch_lines = "\n".join(f"- `fallback-archive={a}`" for a in archives) or "- (none)"
    stats = modlock.get("stats", {})
    eng = modlock.get("engine", {})
    if bundled_cfg:
        client_step, launch_no = "", "2"
        if bundled_client_cfg:
            client_step = (
                f"\n## 2. `{bundled_client_cfg}`  →  your TES3MP install folder (next to `tes3mp.exe`)\n\n"
                f"Drop it in, replacing the one there. It's already pointed at the server; just add the "
                f"**password** (from your invite) on the `password =` line.\n")
            launch_no = "3"
        connect_to = f" to `{server_address}`" if server_address else ""
        return f"""# {title}

You've unzipped the canon. **The ready config files are right here in this folder** -- place each,
edit the one noted bit, connect. Nothing else to install.

## 1. `{bundled_cfg}`  →  your OpenMW config folder

- **Windows:** `Documents\\My Games\\OpenMW\\`
- **macOS:** `~/Library/Preferences/openmw/`
- **Linux:** `~/.config/openmw/`

Merge it into any `openmw.cfg` already there (keep the `fallback=`/settings your OpenMW wizard added
-- that's your weather + lighting). Then open it and **uncomment the two `data=` lines for your OS**
(delete the leading `#`), fixing the paths: your vanilla Morrowind `Data Files`, then this unzipped
folder.
{client_step}
## {launch_no}. Launch `tes3mp.exe`

You're in{connect_to}. Done.

> **Don't** drop a stray `.esp`/`.esm` into a `data=` folder -- TES3MP counts every plugin and
> rejects a client whose count differs from the server's.

<details><summary>Full load order ({stats.get('plugins', n)} plugins -- already baked into <code>{bundled_cfg}</code>)</summary>

{arch_lines}

{order}
</details>

---
*Generated by chim `mod_export`. Re-roll changes these files; always use the latest.*
"""

    # fallback: the older self-serve doc (no pre-rolled config)
    connect = f"\n5. **Connect** to `{server_address}` in the TES3MP browser.\n" if server_address else ""
    verify = f"\n**Verify** `{zip_name}` sha256 = `{zip_sha256}`\n" if zip_sha256 else ""
    return f"""# {title} — mod bundle

One canonical mod set; the server and every client run it byte-identically. **Wipe a canon dir,
unzip `{zip_name}` into it**, point your `openmw.cfg` at it (a `data=` line for the canon dir + the
archives below), and list the {stats.get('plugins', n)} plugins in this exact order.
{connect}
Archives:

{arch_lines}

Load order:

{order}
{verify}
---
*Generated by chim `mod_export`.*
"""


def render_client_cfg(modlock: Dict, data_dirs: Sequence[str] = (), *,
                      header: Optional[str] = None,
                      data_options: Optional[Sequence[Tuple[str, Sequence[str]]]] = None) -> str:
    """Generate a client ``openmw.cfg`` body from a modlock -- the INVERSE of the roll. Emits the
    ``data=`` dirs, then ``fallback-archive=`` / ``groundcover=`` / ``content=`` verbatim from the
    roll, so a client loads exactly the plugins + order the server enforces and the handshake matches.

    ``data_dirs`` are emitted as ACTIVE ``data=`` lines (the exact-paths case: deploying to a known
    host). ``data_options`` -- a list of ``(os_label, [paths])`` -- instead emits each set as a
    COMMENTED ``#data=`` block under a ``# ---- <os_label> ----`` heading: the distributable config a
    friend uncomments for their own OS. Pass either (or both)."""
    lines = [header or "# TES3MP canon client config -- generated by chim from the modlock"]
    for label, paths in (data_options or ()):
        lines.append(f"# ---- {label} ----")
        lines += [f"#data={loadorder._escape_cfg(p)}" for p in paths]
    lines += [f"data={loadorder._escape_cfg(d)}" for d in data_dirs]
    lines += [f"fallback-archive={a}" for a in modlock.get("archives", [])]
    lines += [f"groundcover={g}" for g in modlock.get("groundcover", [])]
    lines += [f"content={c['name']}" for c in modlock.get("content", [])]
    return "\n".join(lines) + "\n"


def render_client_default_cfg(server_address: str, *, password: Optional[str] = None,
                              log_level: int = 1) -> str:
    """Generate a ``tes3mp-client-default.cfg`` (the file that sits next to ``tes3mp.exe``) preset
    to this server, so a friend who drops it in auto-connects on launch. ``server_address`` is
    ``host`` or ``host:port`` -- ``destinationAddress`` takes the host only, the port is its own key.
    ``password`` fills the ``password=`` line when given; otherwise it's left blank with a note (a
    friend adds it from their invite -- we don't bake a shared secret into a public torrent)."""
    host, _, port = server_address.partition(":")
    port = port or "25565"
    pw_line = (f"password = {password}" if password else
               "password =   # <-- put the server password here (from your invite)")
    return f"""[General]
destinationAddress = {host}
port = {port}
{pw_line}
# 0 - Verbose (spam), 1 - Info, 2 - Warnings, 3 - Errors, 4 - Only fatal errors
logLevel = {log_level}

[Master]
address = master.tes3mp.com
port = 25561

[Chat]
# Use https://wiki.libsdl.org/SDL_Keycode to find the correct key codes when rebinding
keySay = Y
keyChatMode = F2
x = 0
y = 0
w = 390
h = 250
delay = 5.0
"""


def render_deploy_ps1(zip_url: str, sha256: str, canon_dir: str, *,
                      expect_present: Sequence[str] = ()) -> str:
    """A Windows PowerShell script that (re)deploys a rolled canon to a TES3MP client host:
    curl-pull ``zip_url`` (resumable + retry), verify ``sha256``, wipe ``canon_dir``, extract
    (``tar``), delete the zip, then assert each ``expect_present`` path exists under the canon.
    The client ``openmw.cfg`` (:func:`render_client_cfg`) is pushed separately -- an asset-only
    reroll needs only this script; a plugin-set change needs both."""
    checks = "\n".join(
        'Write-Output ("present {p}: " + (Test-Path (Join-Path "{cd}" "{p}")))'.format(p=p, cd=canon_dir)
        for p in expect_present)
    return '''$ErrorActionPreference = "Stop"
$zip = "{canon}.zip"
curl.exe -L -C - --retry 15 --retry-delay 3 --retry-all-errors -s -S -o $zip "{url}"
if ($LASTEXITCODE -ne 0) {{ throw "download failed ($LASTEXITCODE)" }}
$h = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
if ($h -ne "{sha}") {{ throw "sha256 mismatch: $h" }}
Write-Output "sha256 OK"
Remove-Item "{canon}" -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory "{canon}" | Out-Null
tar.exe -xf $zip -C "{canon}"
if ($LASTEXITCODE -ne 0) {{ throw "extract failed ($LASTEXITCODE)" }}
Remove-Item $zip -Force
{checks}
Write-Output "deployed {canon}"
'''.format(canon=canon_dir, url=zip_url, sha=sha256.lower(), checks=checks)


# The header + per-OS data= options chim stamps on the pre-rolled openmw.cfg it ships in every zip.
# INSTALL.md says WHERE to place the file; this header says the one edit the friend makes in it.
_CLIENT_CFG_HEADER = (
    "# TES3MP canon openmw.cfg -- pre-rolled by chim.\n"
    "#   * See INSTALL.md for where to place this file (your OpenMW config dir); MERGE it into any\n"
    "#     existing openmw.cfg and keep the fallback=/settings your OpenMW wizard added.\n"
    "#   * The one edit: UNCOMMENT the two data= lines for YOUR OS below (delete the leading #) and\n"
    "#     fix the paths -- your vanilla Morrowind 'Data Files', then the folder you unzipped the\n"
    "#     canon into. Everything below data= (archives + load order) is already correct; leave it."
)

# Per-OS example data= pairs (vanilla Data Files, then the unzipped canon dir) chim emits COMMENTED
# in the distributable openmw.cfg -- the friend uncomments the pair for their platform.
_OS_DATA_OPTIONS = (
    ("Windows", ("C:/Program Files (x86)/Steam/steamapps/common/Morrowind/Data Files", "C:/MW-Canon")),
    ("macOS", ("/Users/YOU/Games/Morrowind/Data Files", "/Users/YOU/Games/MW-Canon")),
    ("Linux", ("/home/YOU/Games/Morrowind/Data Files", "/home/YOU/Games/MW-Canon")),
)


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #

def roll(cfg_path: str, out_dir: str, *, bundle_dirs: Sequence[str],
         data_dirs: Optional[Sequence[str]] = None, engine: Optional[Dict] = None,
         allow_lua: Optional[bool] = None, enforce_checksums: bool = True,
         zip_name: str = "canon.zip", server_address: Optional[str] = None,
         title: str = "Morrowind Multiplayer", expect_plugins: Optional[int] = None,
         forbid_substrings: Sequence[str] = (), password: Optional[str] = None) -> Dict:
    """Do a full roll: build the modlock, write all four artifacts into ``out_dir``, return a
    report. ``out_dir`` is the git seam (commit it) + the place the zip is uploaded from.

    The chosen ``enforce_checksums`` is **persisted into the modlock** so baar-dau's
    ``modlist_apply`` derives an identical ``requiredDataFiles.json`` server-side. Before
    writing anything the roll runs :func:`assert_roll_sane` (``expect_plugins`` +
    ``forbid_substrings`` cut-list guard + bundle-membership) and, after zipping, re-CRCs every
    shipped plugin from the zip -- either raises :class:`ModlockError` rather than emit a canon
    that would silently reject clients or ship un-hashed bytes.
    """
    dds = list(data_dirs if data_dirs is not None else loadorder.read_data_dirs(cfg_path))
    modlock = build_modlock(cfg_path, bundle_dirs=bundle_dirs, data_dirs=dds,
                            engine=engine, allow_lua=allow_lua)
    modlock["enforce_checksums"] = enforce_checksums
    assert_roll_sane(modlock, bundle_dirs=bundle_dirs, data_dirs=dds,
                     expect_plugins=expect_plugins, forbid_substrings=forbid_substrings)

    os.makedirs(out_dir, exist_ok=True)
    modlock_path = os.path.join(out_dir, "modlock.json")
    with open(modlock_path, "w", encoding="utf-8") as fh:
        json.dump(modlock, fh, indent=2)

    rdf = required_data_files(modlock, enforce_checksums=enforce_checksums)
    rdf_path = os.path.join(out_dir, "requiredDataFiles.json")
    with open(rdf_path, "w", encoding="utf-8") as fh:
        json.dump(rdf, fh, indent=2)

    # Pre-roll the config files a friend needs INTO the zip (+ as siblings): a ready openmw.cfg with
    # per-OS data= options they uncomment, a tes3mp-client-default.cfg preset to the server, and a
    # dead-simple INSTALL.md ("place each file, edit the one noted bit"). The in-zip INSTALL.md omits
    # the zip's own sha (a file inside the zip can't name the zip's hash); the download sha rides on
    # the roll's return + the JOIN doc.
    openmw_cfg = render_client_cfg(modlock, header=_CLIENT_CFG_HEADER, data_options=_OS_DATA_OPTIONS)
    client_default = (render_client_default_cfg(server_address, password=password)
                      if server_address else None)
    install_md = render_install_md(
        modlock, zip_name=zip_name, server_address=server_address, title=title,
        bundled_cfg="openmw.cfg",
        bundled_client_cfg=("tes3mp-client-default.cfg" if client_default else None))
    extra = {"openmw.cfg": openmw_cfg, "INSTALL.md": install_md}
    if client_default:
        extra["tes3mp-client-default.cfg"] = client_default
    # Ship the incremental-update clients IN the zip (and as out_dir siblings, which is how they
    # reach the server for standalone download): a cold-installed friend then already has the
    # update tool, and each run self-verifies files against this same modlock.json. They sit on
    # the sync's own IGNORE list so a sync never deletes them.
    for _name in ("canon-sync.ps1", "canon-sync.sh"):
        _p = os.path.join(_CLIENTS_DIR, _name)
        if not os.path.isfile(_p):
            raise ModlockError("sync client missing: %s (the canon zip must ship it)" % _p)
        with open(_p, "r", encoding="utf-8") as _fh:
            extra[_name] = _fh.read()

    zip_path = os.path.join(out_dir, zip_name)
    zip_rep = write_bundle_zip(bundle_dirs, zip_path, extra_files=extra)
    bad = verify_zip_crc(zip_path, modlock)
    if bad:
        raise ModlockError("zip CRC != modlock CRC for shipped plugins: %s" % bad)

    # siblings in out_dir (committed): byte-identical to the copies that went into the zip.
    for name, text in extra.items():
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    return {
        "out_dir": out_dir,
        "modlock": modlock_path,
        "required_data_files": rdf_path,
        "zip": zip_rep,
        "install_md": os.path.join(out_dir, "INSTALL.md"),
        "client_cfg": os.path.join(out_dir, "openmw.cfg"),
        "client_default_cfg": (os.path.join(out_dir, "tes3mp-client-default.cfg")
                               if client_default else None),
        "bundled": zip_rep.get("extra_files", []),
        "stats": modlock["stats"],
        "plugins": [c["name"] for c in modlock["content"]],
        "enforce_checksums": enforce_checksums,
        "assertions": "passed",
    }


# --------------------------------------------------------------------------- #
# verify (byte-identity of a Data Files against a modlock)
# --------------------------------------------------------------------------- #

def verify(modlock: Dict, bundle_dirs: Sequence[str]) -> Dict:
    """Re-hash ``bundle_dirs`` and compare to a modlock's ``files`` -- the byte-identity check.

    Returns ``{ok, matched, mismatched, missing, extra}``: ``mismatched`` files differ in
    bytes, ``missing`` are in the modlock but absent on disk, ``extra`` are on disk but not in
    the modlock (a local addition that would break byte-identity). Read-only."""
    want = {f["path"]: f for f in modlock.get("files", [])}
    have: Dict[str, str] = {}
    for d in bundle_dirs:
        for rel, ap in _walk_files(d):
            have[rel] = ap  # later dir wins, matching the bundle merge
    matched: List[str] = []
    mismatched: List[str] = []
    for rel, meta in want.items():
        ap = have.get(rel)
        if ap is None:
            continue
        sha, _crc, _size = hash_file(ap)
        (matched if sha == meta["sha256"] else mismatched).append(rel)
    missing = sorted(set(want) - set(have))
    extra = sorted(set(have) - set(want))
    return {
        "ok": not mismatched and not missing and not extra,
        "matched": len(matched),
        "mismatched": sorted(mismatched),
        "missing": missing,
        "extra": extra,
    }


__all__ = [
    "ModlockError", "hash_file", "crc_hex", "build_modlock", "required_data_files",
    "assert_roll_sane", "verify_zip_crc", "write_bundle_zip", "render_install_md",
    "render_client_cfg", "render_client_default_cfg", "render_deploy_ps1", "roll",
    "verify", "MODLOCK_VERSION", "VANILLA_CRCS",
]
