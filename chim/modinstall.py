"""Headless mod installation for OpenMW / Morrowind.

Generalises the "extract archive -> drop assets in ``Data Files`` -> register in
``openmw.cfg``" dance (done by hand a dozen times while porting the Rotfern race)
into one reversible operation, in chim's lock/backup/verify spirit:

* :func:`extract_archive` -> :func:`find_data_root` -> :func:`plan_install`
  discover *what* a mod would install (a plugin, a BSA, loose assets), with an
  ``include``/``exclude`` glob filter so you can take **only the assets you use**.
* :func:`install` copies the files, registers plugins (``content=``) and BSAs
  (``fallback-archive=``) in ``openmw.cfg`` (idempotent, order-correct), and writes
  a JSON **manifest** of exactly what it added.
* :func:`uninstall` reads that manifest and removes precisely those files + cfg
  lines (never touching pre-existing files) -- so an install is fully reversible.
* :func:`extract_bsa_assets` pulls a *subset* of a mod's BSA out as loose files
  (the surgical "only ``de_kids`` meshes" path), backed by :class:`chim.tes3.bsa.Bsa`.

TES3-only for now (OpenMW's ``openmw.cfg``); the same shape extends to Skyrim's
``plugins.txt`` later.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .tes3.bsa import Bsa

PLUGIN_EXTS = (".esp", ".esm", ".esl", ".omwaddon")
#: Directory names that mark an extracted tree as a game data root (Morrowind + Skyrim).
ASSET_DIRS = {"meshes", "textures", "icons", "sound", "bookart", "fonts", "splash",
              "music", "video", "mwscripts", "distantland", "shaders",
              "scripts", "seq", "interface", "skse", "source", "strings",
              "shadersfx", "grass", "dyndolod", "materials", "facegendata"}
#: A directory with one of these names (case-insensitive) IS the data root.
ROOT_DIR_NAMES = {"data files", "data"}


class ModInstallError(Exception):
    """A mod could not be extracted / located / installed."""


# --------------------------------------------------------------------------- #
# Archive extraction + Data Files discovery
# --------------------------------------------------------------------------- #

def find_7z() -> Optional[str]:
    """Locate a 7-Zip binary (incl. the one Vortex bundles), or None."""
    from shutil import which
    for c in (r"C:\Program Files\7-Zip\7z.exe",
              r"C:\Program Files\Vortex\resources\app.asar.unpacked\node_modules\7z-bin\win32\7z.exe",
              "/opt/homebrew/bin/7z", "/usr/local/bin/7z", "/usr/bin/7z"):
        if os.path.exists(c):
            return c
    return which("7z") or which("7za") or which("7zz")


def extract_archive(archive: str, dest: str, seven_zip: Optional[str] = None) -> str:
    """Extract ``archive`` (.zip via stdlib; .7z/.rar via a 7z binary) into ``dest``."""
    os.makedirs(dest, exist_ok=True)
    if archive.lower().endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
        return dest
    sz = seven_zip or find_7z()
    if not sz:
        raise ModInstallError(f"no 7z binary available to extract {archive!r}")
    r = subprocess.run([sz, "x", archive, "-o" + dest, "-y"], capture_output=True, text=True)
    if r.returncode != 0:
        raise ModInstallError(f"extract failed ({r.returncode}): {(r.stderr or r.stdout)[:200]}")
    return dest


def find_data_root(tree: str) -> str:
    """Find the Morrowind ``Data Files`` root inside an extracted mod ``tree``.

    Prefers a directory literally named ``Data Files``; otherwise the shallowest
    directory that directly holds a plugin/BSA or a standard asset subdir
    (``Meshes``/``Textures``/…). Raises if none is found."""
    fallback: Optional[str] = None
    for dirpath, dirs, files in os.walk(tree):
        if os.path.basename(dirpath).lower() in ROOT_DIR_NAMES:
            return dirpath
        low_dirs = {d.lower() for d in dirs}
        has_plugin = any(f.lower().endswith(PLUGIN_EXTS) or f.lower().endswith(".bsa") for f in files)
        if (has_plugin or (low_dirs & ASSET_DIRS)) and fallback is None:
            fallback = dirpath
    if fallback is None:
        raise ModInstallError("no Data Files root found (no plugin/BSA/asset dirs in the archive)")
    return fallback


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #

def _kind(fname: str) -> str:
    low = fname.lower()
    if low.endswith(PLUGIN_EXTS):
        return "plugin"
    if low.endswith(".bsa"):
        return "archive"
    return "asset"


@dataclass
class PlanItem:
    src: str      # absolute source path in the extracted tree
    rel: str      # path relative to the Data Files root (== install target under data_dir)
    kind: str     # 'plugin' | 'archive' | 'asset'


@dataclass
class InstallPlan:
    data_root: str
    items: List[PlanItem] = field(default_factory=list)

    @property
    def plugins(self) -> List[str]:
        return [os.path.basename(i.rel) for i in self.items if i.kind == "plugin"]

    @property
    def archives(self) -> List[str]:
        return [os.path.basename(i.rel) for i in self.items if i.kind == "archive"]

    def summary(self) -> Dict[str, Any]:
        return {"data_root": self.data_root, "file_count": len(self.items),
                "plugins": self.plugins, "archives": self.archives,
                "assets": sum(1 for i in self.items if i.kind == "asset")}


def _match(rel: str, pats: Sequence[str]) -> bool:
    rl = rel.lower().replace("\\", "/")
    return any(fnmatch.fnmatch(rl, p.lower().replace("\\", "/")) for p in pats)


def plan_install(data_root: str, include: Optional[Sequence[str]] = None,
                 exclude: Optional[Sequence[str]] = None) -> InstallPlan:
    """Enumerate the files under ``data_root`` this mod would install, filtered by
    ``include`` / ``exclude`` globs (matched against the Data-Files-relative path)."""
    items: List[PlanItem] = []
    for dirpath, _, files in os.walk(data_root):
        for f in files:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, data_root)
            if include and not _match(rel, include):
                continue
            if exclude and _match(rel, exclude):
                continue
            items.append(PlanItem(full, rel, _kind(f)))
    return InstallPlan(data_root, items)


# --------------------------------------------------------------------------- #
# openmw.cfg registration
# --------------------------------------------------------------------------- #

def register_openmw(cfg_path: str, plugins: Sequence[str], archives: Sequence[str],
                    dry_run: bool = False) -> List[str]:
    """Append ``fallback-archive=`` (for BSAs) then ``content=`` (for plugins) to
    ``openmw.cfg``, idempotently. Appends at EOF so mod entries load after vanilla.
    Returns the lines it added (or would add)."""
    existing = {l.strip().lower() for l in open(cfg_path, encoding="utf-8", errors="replace")}
    to_add: List[str] = []
    for a in archives:
        line = f"fallback-archive={a}"
        if line.lower() not in existing:
            to_add.append(line)
    for p in plugins:
        line = f"content={p}"
        if line.lower() not in existing:
            to_add.append(line)
    if to_add and not dry_run:
        raw = open(cfg_path, encoding="utf-8", errors="replace").read()
        with open(cfg_path, "a", encoding="utf-8") as f:
            f.write(("" if raw.endswith("\n") or not raw else "\n") + "\n".join(to_add) + "\n")
    return to_add


def register_skyrim(plugins_txt: str, plugins: Sequence[str], archives: Sequence[str],
                    dry_run: bool = False) -> List[str]:
    """Activate ``plugins`` in Skyrim's ``plugins.txt`` (a leading ``*`` marks a
    plugin active), idempotently, appended so they load last. Skyrim auto-loads a
    BSA named after an active plugin, so ``archives`` need no separate line.
    Returns the lines added."""
    lines = (open(plugins_txt, encoding="utf-8", errors="replace").read().splitlines()
             if os.path.exists(plugins_txt) else [])
    present = {l.strip().lstrip("*").strip().lower() for l in lines if l.strip()}
    to_add = [f"*{p}" for p in plugins if p.lower() not in present]
    if to_add and not dry_run:
        raw = open(plugins_txt, encoding="utf-8", errors="replace").read() if os.path.exists(plugins_txt) else ""
        d = os.path.dirname(plugins_txt)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(plugins_txt, "a", encoding="utf-8") as f:
            f.write(("" if (not raw or raw.endswith("\n")) else "\n") + "\n".join(to_add) + "\n")
    return to_add


def _register(game: str, cfg_path: str, plugins: Sequence[str], archives: Sequence[str],
              dry_run: bool) -> List[str]:
    if game.lower() == "skyrim":
        return register_skyrim(cfg_path, plugins, archives, dry_run=dry_run)
    return register_openmw(cfg_path, plugins, archives, dry_run=dry_run)


def _remove_cfg_lines(cfg_path: str, lines: Sequence[str]) -> int:
    drop = {l.strip().lower() for l in lines}
    kept, removed = [], 0
    for l in open(cfg_path, encoding="utf-8", errors="replace").read().splitlines():
        if l.strip().lower() in drop:
            removed += 1
        else:
            kept.append(l)
    if removed:
        open(cfg_path, "w", encoding="utf-8").write("\n".join(kept) + "\n")
    return removed


# --------------------------------------------------------------------------- #
# Install / uninstall
# --------------------------------------------------------------------------- #

def install(plan: InstallPlan, data_dir: str, cfg_path: str, name: str,
            manifest_dir: str, game: str = "openmw", dry_run: bool = False) -> Dict[str, Any]:
    """Copy the plan's files into ``data_dir``, register plugins/BSAs in
    ``cfg_path`` (``game="openmw"`` -> ``openmw.cfg``; ``game="skyrim"`` ->
    ``plugins.txt``), and write a ``<name>.json`` manifest to ``manifest_dir``.
    ``dry_run`` reports without writing anything. Re-installing the same ``name``
    preserves ownership (files/cfg lines chim already added stay chim-owned, so a
    later uninstall still reverses them)."""
    # what a prior install of this name already owns (so re-install stays reversible)
    prior_owned: set = set()
    prior_cfg: List[str] = []
    prior_path = os.path.join(manifest_dir, name + ".json")
    if os.path.exists(prior_path):
        try:
            prior = json.load(open(prior_path, encoding="utf-8"))
            prior_owned = {c["rel"] for c in prior.get("files", []) if not c.get("preexisting")}
            prior_cfg = list(prior.get("cfg_added", []))
        except Exception:
            pass
    copied: List[Dict[str, Any]] = []
    for it in plan.items:
        dst = os.path.join(data_dir, it.rel)
        pre = os.path.exists(dst) and it.rel not in prior_owned
        if not dry_run:
            d = os.path.dirname(dst)
            if d:
                os.makedirs(d, exist_ok=True)
            if os.path.exists(dst):
                try:
                    os.chmod(dst, 0o666)
                except OSError:
                    pass
            shutil.copy2(it.src, dst)
        copied.append({"rel": it.rel, "kind": it.kind, "preexisting": pre})
    newly = _register(game, cfg_path, plan.plugins, plan.archives, dry_run=dry_run)
    cfg_added = list(dict.fromkeys(prior_cfg + newly))
    manifest = {"name": name, "game": game, "data_dir": data_dir, "cfg": cfg_path,
                "plugins": plan.plugins, "archives": plan.archives,
                "files": copied, "cfg_added": cfg_added,
                "new_file_count": sum(1 for c in copied if not c["preexisting"]),
                "dry_run": dry_run}
    if not dry_run:
        os.makedirs(manifest_dir, exist_ok=True)
        with open(os.path.join(manifest_dir, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1)
    return manifest


def install_archive(archive: str, data_dir: str, cfg_path: str, manifest_dir: str,
                    name: Optional[str] = None, game: str = "openmw",
                    include: Optional[Sequence[str]] = None,
                    exclude: Optional[Sequence[str]] = None, tmp_dir: Optional[str] = None,
                    seven_zip: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    """One-shot: extract ``archive`` -> find its data root -> plan (filtered) ->
    install for ``game`` (``openmw``/``skyrim``). ``name`` defaults to the stem."""
    name = name or os.path.splitext(os.path.basename(archive))[0]
    tmp = tmp_dir or os.path.join(os.path.dirname(os.path.abspath(archive)), "_chim_extract_" + name)
    extract_archive(archive, tmp, seven_zip=seven_zip)
    plan = plan_install(find_data_root(tmp), include=include, exclude=exclude)
    result = install(plan, data_dir, cfg_path, name, manifest_dir, game=game, dry_run=dry_run)
    result["extracted_to"] = tmp
    return result


def extract_bsa_assets(bsa_path: str, data_dir: str, name: str, manifest_dir: str,
                       patterns: Optional[Sequence[str]] = None,
                       names: Optional[Sequence[str]] = None,
                       dry_run: bool = False) -> Dict[str, Any]:
    """Surgically pull a subset of a mod's BSA out as loose files under ``data_dir``
    (the "only the assets we use" path), and record a manifest. Backed by
    :class:`chim.tes3.bsa.Bsa`."""
    bsa = Bsa(path=bsa_path)
    sel: List[str] = []
    if patterns:
        sel += bsa.find(patterns)
    if names:
        want = {n.lower().replace("/", "\\") for n in names}
        sel += [n for n in bsa.names() if n.lower() in want]
    if not patterns and not names:
        sel = bsa.names()
    hits = list(dict.fromkeys(sel))   # dedup, preserve archive order
    copied: List[Dict[str, Any]] = []
    for n in hits:
        rel = n.replace("\\", os.sep)
        dst = os.path.join(data_dir, rel)
        pre = os.path.exists(dst)
        if not dry_run:
            bsa.extract(n, dst)
        copied.append({"rel": rel, "kind": "asset", "preexisting": pre})
    manifest = {"name": name, "data_dir": data_dir, "cfg": None, "plugins": [], "archives": [],
                "files": copied, "cfg_added": [],
                "new_file_count": sum(1 for c in copied if not c["preexisting"]), "dry_run": dry_run}
    if not dry_run:
        os.makedirs(manifest_dir, exist_ok=True)
        with open(os.path.join(manifest_dir, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1)
    return manifest


def uninstall(manifest: Dict[str, Any], cfg_path: Optional[str] = None) -> Dict[str, Any]:
    """Reverse an install from its manifest: delete the files it *added* (never a
    pre-existing one) and drop the cfg lines it added."""
    data_dir = manifest["data_dir"]
    files_removed = 0
    for c in manifest.get("files", []):
        if c.get("preexisting"):
            continue
        p = os.path.join(data_dir, c["rel"])
        if os.path.exists(p):
            try:
                os.chmod(p, 0o666)
                os.remove(p)
                files_removed += 1
            except OSError:
                pass
    cfg = cfg_path or manifest.get("cfg")
    cfg_removed = 0
    if cfg and manifest.get("cfg_added") and os.path.exists(cfg):
        cfg_removed = _remove_cfg_lines(cfg, manifest["cfg_added"])
    return {"files_removed": files_removed, "cfg_lines_removed": cfg_removed}


def load_manifest(manifest_dir: str, name: str) -> Dict[str, Any]:
    path = os.path.join(manifest_dir, name + ".json")
    if not os.path.exists(path):
        raise ModInstallError(f"no install manifest for {name!r} in {manifest_dir!r}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def list_manifests(manifest_dir: str) -> List[str]:
    if not os.path.isdir(manifest_dir):
        return []
    return sorted(os.path.splitext(f)[0] for f in os.listdir(manifest_dir) if f.endswith(".json"))
