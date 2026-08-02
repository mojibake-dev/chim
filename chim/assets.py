"""Asset-closure resolver: the exact set of files a plugin set actually references.

The "only the assets we use" rule (chim's ethos), applied to a whole modlist. Given
the content plugins + the dir(s) their loose assets live in, walk:

    plugins -> direct refs (MODL meshes, ICON icons, LTEX land textures, SOUN sounds)
            -> for each referenced mesh, the textures that NIF references

and return the **closure**: every file the plugins genuinely need, and nothing else.
Everything the plugins don't point at -- cut-mod leftovers, orphans from long-removed
mods, a mod manager's `overwrite` graveyard -- is simply never in the closure, so a
roll built from it is provably clean (and smaller) without trusting the source dir to
be tidy.

Reuses the mesh/texture scanners in :mod:`chim.modinstall` (``mesh_texture_refs``,
``TEX_EXTS``) and the TES3 parser in :mod:`chim.tes3`. Resolution is
extension-substituting for textures (a mesh's ``.tga`` ref matches a loose ``.dds``,
as OpenMW substitutes) and relies on the case-insensitive filesystem of both targets
(Windows source, macOS roll host). Unresolved refs come back as ``missing`` -- the
gaps to fill from clean mod archives.
"""
from __future__ import annotations

import os
import shutil
from typing import Dict, List, Sequence, Set

from . import modinstall
from .tes3 import parse_plugin

TEX_EXTS = modinstall.TEX_EXTS  # (.dds, .tga, .bmp)


def _cstr(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("cp1252", "replace").strip()


def _norm(rel: str) -> str:
    return rel.replace("\\", "/").lstrip("/")


def plugin_asset_refs(plugin) -> Dict[str, Set[str]]:
    """Direct asset refs in a parsed TES3 plugin, grouped by kind.

    ``meshes`` = ``MODL`` paths (relative to ``Meshes\\``, on many record types);
    ``icons`` = ``ICON`` inventory-icon paths; ``textures`` = ``LTEX`` land-texture
    names; ``sounds`` = ``SOUN`` sound files. Paths are returned as stored.
    """
    meshes: Set[str] = set()
    icons: Set[str] = set()
    textures: Set[str] = set()
    sounds: Set[str] = set()
    for rec in plugin.records:
        rt = rec.type
        for sr in rec.subrecords:
            n = sr.name
            if n == "MODL":
                v = _cstr(sr.data)
                if v:
                    meshes.add(v)
            elif n == "ICON":
                v = _cstr(sr.data)
                if v:
                    icons.add(v)
            elif n == "DATA" and rt == "LTEX":
                v = _cstr(sr.data)
                if v:
                    textures.add(v)
            elif n == "FNAM" and rt == "SOUN":
                v = _cstr(sr.data)
                if v:
                    sounds.add(v)
    return {"meshes": meshes, "icons": icons, "textures": textures, "sounds": sounds}


#: Subdirs a plugin file may sit in under a data dir (some managers nest them). NOT a
#: recursive walk -- a bounded, explicit list, so resolving 158 plugins stays O(dirs).
PLUGIN_SUBDIRS = ("", "03 TR Plugins", "00 Core")


def find_plugin_file(name: str, data_dirs: Sequence[str], subdirs: Sequence[str] = PLUGIN_SUBDIRS):
    """Locate a plugin file ``name`` across ``data_dirs`` (+ a few known subdirs)."""
    for d in data_dirs:
        for sub in subdirs:
            p = os.path.join(d, sub, name)
            if os.path.isfile(p):
                return p
    return None


def _find(data_dirs: Sequence[str], subdir: str, rel: str):
    """Find ``<data_dir>/<subdir>/<rel>`` across the dirs (exact; case-insensitive FS)."""
    rp = _norm(rel)
    for d in data_dirs:
        p = os.path.join(d, subdir, rp) if subdir else os.path.join(d, rp)
        if os.path.isfile(p):
            return p
    return None


def _find_tex(data_dirs: Sequence[str], rel: str, subdirs=("textures",)):
    """Resolve a ``Textures``-relative ref with extension substitution."""
    stem = _norm(os.path.splitext(rel)[0])
    for d in data_dirs:
        for sub in subdirs:
            base = os.path.join(d, sub, stem)
            for e in TEX_EXTS:
                if os.path.isfile(base + e):
                    return base + e
    return None


def resolve_closure(plugin_paths: Sequence[str], data_dirs: Sequence[str], *,
                    include_sounds: bool = True, progress=None) -> Dict:
    """Resolve the used-file closure of ``plugin_paths`` against ``data_dirs``.

    Returns ``used_files`` (sorted abs paths -- the plugins + every mesh/texture/icon/
    land/sound they reference, transitively through meshes), ``missing`` (refs that did
    not resolve, grouped by kind -- the gaps), and ``stats``. ``progress(msg)`` is called
    at phase boundaries + periodically through the slow mesh scan (for a live log)."""
    _p = progress or (lambda *_: None)
    data_dirs = [os.path.abspath(d) for d in data_dirs]
    mesh_refs: Set[str] = set()
    icon_refs: Set[str] = set()
    tex_refs: Set[str] = set()
    sound_refs: Set[str] = set()
    _p("parsing %d plugins" % len(plugin_paths))
    for i, pp in enumerate(plugin_paths):
        r = plugin_asset_refs(parse_plugin(open(pp, "rb").read()))
        mesh_refs |= r["meshes"]
        icon_refs |= r["icons"]
        tex_refs |= r["textures"]
        sound_refs |= r["sounds"]
        if i % 25 == 0:
            _p("parsed %d/%d plugins" % (i + 1, len(plugin_paths)))

    used: Set[str] = set()
    missing: Dict[str, Set[str]] = {"meshes": set(), "textures": set(), "icons": set(), "sounds": set()}

    _p("resolving %d mesh refs" % len(mesh_refs))
    mesh_files: List[str] = []
    for mr in mesh_refs:
        f = _find(data_dirs, "meshes", mr)
        if f:
            used.add(f)
            mesh_files.append(f)
        else:
            missing["meshes"].add(mr)

    _p("scanning %d meshes for texture refs" % len(mesh_files))
    for i, mf in enumerate(mesh_files):  # meshes -> the textures their NIF references
        try:
            tex_refs |= modinstall.mesh_texture_refs(mf)
        except OSError:
            pass
        if i % 500 == 0:
            _p("scanned %d/%d meshes" % (i + 1, len(mesh_files)))

    for tr in tex_refs:
        f = _find_tex(data_dirs, tr)
        if f:
            used.add(f)
        else:
            missing["textures"].add(tr)

    for ir in icon_refs:
        f = _find_tex(data_dirs, ir, subdirs=("icons", "textures"))
        if f:
            used.add(f)
        else:
            missing["icons"].add(ir)

    if include_sounds:
        for sr in sound_refs:
            f = _find(data_dirs, "sound", sr) or _find(data_dirs, "", sr)
            if f:
                used.add(f)
            else:
                missing["sounds"].add(sr)

    used |= {os.path.abspath(p) for p in plugin_paths if os.path.isfile(p)}
    _p("done: %d used files, computing size" % len(used))
    total = sum(os.path.getsize(f) for f in used if os.path.isfile(f))
    return {
        "used_files": sorted(used),
        "missing": {k: sorted(v) for k, v in missing.items()},
        "stats": {
            "plugins": len(plugin_paths),
            "used_files": len(used),
            "mesh_refs": len(mesh_refs),
            "texture_refs": len(tex_refs),
            "missing_meshes": len(missing["meshes"]),
            "missing_textures": len(missing["textures"]),
            "missing_icons": len(missing["icons"]),
            "total_bytes": total,
        },
    }


def _rel_to_dir(path: str, data_dirs: Sequence[str]):
    for d in data_dirs:
        d = os.path.abspath(d)
        if path.startswith(d + os.sep):
            return os.path.relpath(path, d)
    return os.path.basename(path)


def materialize_closure(result: Dict, data_dirs: Sequence[str], dest: str) -> Dict:
    """Copy the resolved closure into ``dest``, preserving each file's data-dir-relative
    path -- i.e. write a **clean** Data Files containing only the used assets. Later
    ``data_dirs`` win on path collisions (the OpenMW override order). Returns a report."""
    data_dirs = [os.path.abspath(d) for d in data_dirs]
    copied = 0
    total = 0
    for f in result["used_files"]:
        rel = _rel_to_dir(f, data_dirs)
        out = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        shutil.copy2(f, out)
        copied += 1
        total += os.path.getsize(f)
    return {"dest": dest, "copied": copied, "total_bytes": total}


def resolve_modlist(cfg: str = None, content=None, data_dirs=None, *, exclude=(),
                    include_sounds: bool = True, progress=None) -> Dict:
    """Resolve the closure for a whole modlist: take ``content`` (plugin names; or read
    ``content=`` from ``cfg``) + ``data_dirs`` (or ``cfg``'s ``data=``), locate each plugin,
    and resolve. Returns :func:`resolve_closure`'s result plus ``plugins_missing`` (names that
    weren't found as files) and ``closure_gb``. The one call ``mod_resolve`` and the CLI share."""
    from . import loadorder
    dd = list(data_dirs) if data_dirs else (loadorder.read_data_dirs(cfg) if cfg else [])
    names = list(content) if content is not None else (loadorder.read_content(cfg) if cfg else [])
    excl = {e.lower() for e in exclude}
    names = [n for n in names if n.lower() not in excl]
    paths, miss = [], []
    for n in names:
        p = find_plugin_file(n, dd)
        (paths if p else miss).append(p or n)
    r = resolve_closure(paths, dd, include_sounds=include_sounds, progress=progress)
    r["plugins_resolved"] = len(paths)
    r["plugins_missing"] = miss
    r["closure_gb"] = round(r["stats"]["total_bytes"] / 1e9, 2)
    return r


def main(argv=None):
    """``python -m chim.assets`` -- resolve a modlist's used-asset closure and write the
    file list. The durable, launch-detached-able form (replaces ad-hoc resolve scripts)."""
    import argparse
    import json
    import time

    ap = argparse.ArgumentParser("chim.assets", description="resolve a modlist's used-asset closure")
    ap.add_argument("--cfg", help="openmw.cfg to read content= + data= from")
    ap.add_argument("--content", help="file of plugin names (one per line); overrides cfg content=")
    ap.add_argument("--data-dir", action="append", default=[], dest="data_dirs",
                    help="asset dir (repeatable); overrides cfg data=")
    ap.add_argument("--exclude", action="append", default=[], help="plugin name to skip (repeatable)")
    ap.add_argument("--out-closure", help="write the resolved file list here")
    ap.add_argument("--out-result", help="write the result JSON here")
    ap.add_argument("--progress", help="append timestamped progress here (else stdout)")
    ap.add_argument("--no-sounds", action="store_true")
    a = ap.parse_args(argv)

    pf = open(a.progress, "w", encoding="utf-8") if a.progress else None

    def log(m):
        line = "%s %s" % (time.strftime("%H:%M:%S"), m)
        (pf.write(line + "\n"), pf.flush()) if pf else print(line, flush=True)

    content = None
    if a.content:
        content = [l.strip() for l in open(a.content, encoding="utf-8") if l.strip()]
    try:
        r = resolve_modlist(cfg=a.cfg, content=content, data_dirs=a.data_dirs or None,
                            exclude=a.exclude, include_sounds=not a.no_sounds, progress=log)
    except Exception as e:
        import traceback
        log("ERROR: " + repr(e))
        log(traceback.format_exc())
        raise
    s = r["stats"]
    if a.out_closure:
        with open(a.out_closure, "w", encoding="utf-8") as f:
            f.writelines(uf + "\n" for uf in r["used_files"])
    if a.out_result:
        with open(a.out_result, "w", encoding="utf-8") as f:
            json.dump({"plugins_resolved": r["plugins_resolved"], "plugins_missing": r["plugins_missing"],
                       "stats": s, "closure_gb": r["closure_gb"],
                       "missing_mesh_sample": r["missing"]["meshes"][:25],
                       "missing_texture_sample": r["missing"]["textures"][:25]}, f, indent=1)
    log("COMPLETE: closure %d files = %.2f GB; missing meshes %d textures %d"
        % (s["used_files"], s["total_bytes"] / 1e9, s["missing_meshes"], s["missing_textures"]))
    if pf:
        pf.close()
    return r


__all__ = ["plugin_asset_refs", "resolve_closure", "materialize_closure",
           "resolve_modlist", "find_plugin_file", "main"]


if __name__ == "__main__":
    main()

