"""OpenMW load-order sorting for chim -- a master-valid topological sort, optionally
guided by mlox's curated rules.

chim's own byte parser (:mod:`chim.tes3`) reads each plugin's ``MAST`` records, so chim
can compute a *master-valid* order headlessly (every plugin after its masters). When
**mlox** (rfuzzo/mlox) is available, its curated ordering becomes the *priority* for that
topological sort: we follow mlox's rules everywhere the master graph allows, but never
emit an order that fails to load (mlox itself occasionally does -- e.g. a base-fixes
``.esm`` placed just before a master it patches). So chim gets mlox's community knowledge
*and* a hard load-safety guarantee.

Pipeline (:func:`sort_load_order`): read ``content=`` from ``openmw.cfg`` -> read each
plugin's masters -> (optional) run mlox for a curated priority -> topological sort ->
optionally write it back (also to a Kezyma base cfg). Preview with ``apply=False``; the
write backs up the cfg first.
"""

from __future__ import annotations

import datetime
import heapq
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from .tes3 import parse_plugin

#: A line in mlox's ``[New Load Order]`` section: ``*012*`` / ``_012_`` / ``*!012*!`` + name.
MLOX_ORDER_RE = re.compile(r"^[*_!]+\d+[*_!]+\s+(.+?)\s*$")
_WARN_TAGS = ("[CONFLICT]", "[REQUIRES]", "[NOTE]", "[Patch]", "[ORDER]", "[DUP]")
_PLUGIN_EXTS = (".esp", ".esm", ".omwaddon")
_MLOX_ASSET = "mlox.exe" if os.name == "nt" else ("mlox_macos" if sys.platform == "darwin" else "mlox_linux")


# --------------------------------------------------------------------------- #
# openmw.cfg content= I/O
# --------------------------------------------------------------------------- #

def read_content(cfg_path: str) -> List[str]:
    """The ``content=`` plugin list from an ``openmw.cfg``, in file order."""
    return [l.split("=", 1)[1].strip()
            for l in open(cfg_path, encoding="utf-8", errors="replace").read().splitlines()
            if l.lower().startswith("content=")]


def write_content(cfg_path: str, order: Sequence[str], backup: bool = True) -> None:
    """Rewrite the ``content=`` block of ``cfg_path`` in ``order`` (only plugins already
    present are re-emitted, at the first ``content=`` line's position); every other line
    is preserved verbatim. Writes a timestamped ``.bak`` first unless ``backup=False``."""
    lines = open(cfg_path, encoding="utf-8", errors="replace").read().splitlines()
    present = {l.split("=", 1)[1].strip().lower() for l in lines if l.lower().startswith("content=")}
    seq = [c for c in order if c.lower() in present]
    out, inserted = [], False
    for l in lines:
        if l.lower().startswith("content="):
            if not inserted:
                out += ["content=%s" % c for c in seq]
                inserted = True
            continue
        out.append(l)
    if backup:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(cfg_path, cfg_path + "." + ts + ".bak")
    open(cfg_path, "w", encoding="utf-8").write("\n".join(out) + "\n")


def _unescape_cfg(value: str) -> str:
    """Decode an OpenMW-cfg quoted value (``data="..."``). OpenMW quotes a value that
    needs it and escapes with ``&`` (``&&`` -> ``&``, ``&"`` -> ``"``); an unquoted value
    is literal to end-of-line."""
    v = value.strip()
    if not v.startswith('"'):
        return v
    out: List[str] = []
    i = 1
    while i < len(v):
        c = v[i]
        if c == "&" and i + 1 < len(v):
            out.append(v[i + 1])
            i += 2
            continue
        if c == '"':
            break
        out.append(c)
        i += 1
    return "".join(out)


def _escape_cfg(path: str) -> str:
    """Inverse of :func:`_unescape_cfg`: quote+escape a path for a ``data=`` line if it
    contains whitespace or an OpenMW special char, else emit it bare (matches how OpenMW
    itself writes the config)."""
    if any(ch in path for ch in ' \t"&'):
        return '"' + path.replace("&", "&&").replace('"', '&"') + '"'
    return path


def _read_axis(cfg_path: str, key: str, *, quoted: bool = False) -> List[str]:
    """The values of one ``key=`` axis from an ``openmw.cfg``, in file order."""
    pre = key.lower() + "="
    vals: List[str] = []
    for l in open(cfg_path, encoding="utf-8", errors="replace").read().splitlines():
        if l.lower().startswith(pre):
            raw = l.split("=", 1)[1]
            vals.append(_unescape_cfg(raw) if quoted else raw.strip())
    return vals


def read_data_dirs(cfg_path: str) -> List[str]:
    """The ``data=`` directory list (mod folders) from an ``openmw.cfg``, unquoted."""
    return _read_axis(cfg_path, "data", quoted=True)


def read_archives(cfg_path: str) -> List[str]:
    """The ``fallback-archive=`` BSA list from an ``openmw.cfg``, in load order."""
    return _read_axis(cfg_path, "fallback-archive")


def read_groundcover(cfg_path: str) -> List[str]:
    """The ``groundcover=`` plugin list from an ``openmw.cfg`` (grass mods)."""
    return _read_axis(cfg_path, "groundcover")


#: The four OpenMW load axes, and whether each value is cfg-quoted (only ``data=`` is).
_AXES = (("data", True), ("content", False), ("fallback-archive", False), ("groundcover", False))


def write_all_axes(cfg_path: str, *, data: Optional[Sequence[str]] = None,
                   content: Optional[Sequence[str]] = None,
                   archives: Optional[Sequence[str]] = None,
                   groundcover: Optional[Sequence[str]] = None,
                   backup: bool = True) -> None:
    """Regenerate any of the four load axes in an ``openmw.cfg``, preserving everything else.

    Each provided axis' block is rewritten at the position of its first existing line (or
    appended if it had none); every other line -- comments, blanks, the other axes, the
    ``[General]``-style settings OpenMW keeps elsewhere -- is emitted verbatim. This is the
    write side ``write_content`` couldn't do (it only touches ``content=``); the roller uses
    it to lay down a whole server ``openmw.cfg`` in one pass. Pass ``None`` to leave an axis
    untouched, ``[]`` to clear it. Writes a timestamped ``.bak`` first unless ``backup=False``."""
    spec = {"data": data, "content": content, "fallback-archive": archives, "groundcover": groundcover}
    quoted = {k: q for k, q in _AXES}
    provided = {k for k, v in spec.items() if v is not None}
    lines = open(cfg_path, encoding="utf-8", errors="replace").read().splitlines()

    def emit(key: str) -> List[str]:
        q = quoted[key]
        return ["%s=%s" % (key, _escape_cfg(v) if q else v) for v in spec[key]]

    out: List[str] = []
    inserted: set = set()
    for l in lines:
        key = l.split("=", 1)[0].strip().lower() if "=" in l else None
        if key in provided:
            if key not in inserted:
                out += emit(key)
                inserted.add(key)
            continue  # drop the old axis line
        out.append(l)
    for key in (k for k, _ in _AXES if k in provided and k not in inserted):
        out += emit(key)  # axis provided but absent originally -> append in canonical order

    if backup:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(cfg_path, cfg_path + "." + ts + ".bak")
    open(cfg_path, "w", encoding="utf-8").write("\n".join(out) + "\n")


def plugin_masters(data_dir: str, name: str) -> List[str]:
    """The master filenames a plugin declares (its ``MAST`` records), or ``[]`` if it isn't
    a parseable TES3 plugin (e.g. a ``.omwscripts`` Lua file, or missing)."""
    p = os.path.join(data_dir, name)
    if not os.path.exists(p) or not name.lower().endswith(_PLUGIN_EXTS):
        return []
    try:
        return [sr.data.split(b"\x00")[0].decode("cp1252", "replace")
                for sr in parse_plugin(open(p, "rb").read()).header.subrecords if sr.name == "MAST"]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Topological sort (the master-validity guarantee)
# --------------------------------------------------------------------------- #

def topo_sort(plugins: Sequence[str], masters: Dict[str, Sequence[str]],
              priority=None) -> List[str]:
    """Order ``plugins`` so every plugin follows its masters (a stable topological sort).
    Ties break by ``priority`` -- a ``{name.lower(): rank}`` map, a callable ``name -> rank``,
    or None (alphabetical). Plugins in a master cycle are appended last. This is what wraps
    mlox's (or any) preferred order to make it load-safe."""
    pset = {p.lower() for p in plugins}
    l2a = {p.lower(): p for p in plugins}
    indeg: Dict[str, int] = defaultdict(int)
    adj: Dict[str, List[str]] = defaultdict(list)
    for p in plugins:
        for m in masters.get(p, []):
            ml = m.lower()
            if ml in pset and ml != p.lower():
                adj[ml].append(p.lower())
                indeg[p.lower()] += 1

    def rank(p: str):
        if priority is None:
            return (0, p.lower())
        if callable(priority):
            return (priority(p), p.lower())
        return (priority.get(p.lower(), 10 ** 9), p.lower())

    heap = [(rank(p), p) for p in plugins if indeg[p.lower()] == 0]
    heapq.heapify(heap)
    out: List[str] = []
    done: set = set()
    while heap:
        _, p = heapq.heappop(heap)
        if p.lower() in done:
            continue
        done.add(p.lower())
        out.append(p)
        for nx in adj[p.lower()]:
            indeg[nx] -= 1
            if indeg[nx] == 0:
                heapq.heappush(heap, (rank(l2a[nx]), l2a[nx]))
    for p in plugins:                       # cycles / leftovers
        if p.lower() not in done:
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# mlox
# --------------------------------------------------------------------------- #

def parse_mlox_output(stdout: str, known: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Extract mlox's proposed load order + warning blocks from its stdout. Returns
    ``(order, warnings)`` -- ``order`` is the plugin sequence in the ``[New Load Order]``
    section (mapped back to the exact names in ``known``); ``warnings`` are the grouped
    ``[CONFLICT]``/``[NOTE]``/... blocks."""
    low = {k.lower(): k for k in known}
    order: List[str] = []
    warnings: List[str] = []
    in_order = False
    block: List[str] = []
    for line in stdout.splitlines():
        if "[New Load Order]" in line:
            in_order = True
            continue
        if "END PROPOSED" in line:
            in_order = False
            continue
        if in_order:
            m = MLOX_ORDER_RE.match(line)
            if m and m.group(1).strip().lower() in low:
                order.append(low[m.group(1).strip().lower()])
        elif any(line.startswith(t) for t in _WARN_TAGS):
            if block:
                warnings.append("\n".join(block))
            block = [line.rstrip()]
        elif block and (line.startswith((" ", "\t")) or not line.strip()):
            block.append(line.rstrip())
        elif block:
            warnings.append("\n".join(block))
            block = []
    if block:
        warnings.append("\n".join(block))
    return order, warnings


def find_mlox() -> Optional[str]:
    """Locate an mlox binary (rfuzzo/mlox) -- chim's scratch dir, PATH, or common spots."""
    from shutil import which
    for c in (r"C:\chimcli\mlox.exe", os.path.expanduser("~/mlox.exe"),
              os.path.expanduser("~/.local/bin/mlox"), "/usr/local/bin/mlox"):
        if os.path.exists(c):
            return c
    return which("mlox") or which("mlox.exe")


def ensure_mlox(dest: Optional[str] = None) -> str:
    """Return a path to mlox, downloading the right rfuzzo/mlox release binary for this
    platform (``mlox.exe`` / ``mlox_macos`` / ``mlox_linux``) if none is found. ~40-95 MB,
    once."""
    found = find_mlox()
    if found:
        return found
    if dest is None:
        dest = r"C:\chimcli\mlox.exe" if os.name == "nt" else os.path.expanduser("~/.local/bin/mlox")
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    url = "https://github.com/rfuzzo/mlox/releases/latest/download/" + _MLOX_ASSET
    req = urllib.request.Request(url, headers={"User-Agent": "chim"})
    with urllib.request.urlopen(req, timeout=300) as r:
        open(dest, "wb").write(r.read())
    if os.name != "nt":
        os.chmod(dest, 0o755)
    return dest


def run_mlox(plugins: Sequence[str], mlox_exe: str, workdir: str,
             nodownload: bool = False) -> Tuple[List[str], List[str]]:
    """Run mlox on ``plugins`` (fed via a Morrowind.ini-format file) in check mode and
    return ``(mlox_order, warnings)``. ``nodownload`` skips mlox's rules-DB refresh."""
    ini = os.path.join(workdir, "chim_loadorder.ini")
    with open(ini, "w", encoding="utf-8") as f:
        f.write("[Game Files]\n")
        for i, p in enumerate(plugins):
            f.write("GameFile%d=%s\n" % (i, p))
    args = [mlox_exe, "-f", ini, "-c"] + (["-n"] if nodownload else [])
    r = subprocess.run(args, capture_output=True, text=True, timeout=600, cwd=workdir)
    return parse_mlox_output(r.stdout, plugins)


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def sort_load_order(cfg_path: str, data_dir: str, *, use_mlox: bool = True,
                    mlox_exe: Optional[str] = None, workdir: Optional[str] = None,
                    priority=None, apply: bool = True,
                    also: Optional[Sequence[str]] = None,
                    nodownload: bool = True) -> Dict:
    """Sort an ``openmw.cfg`` load order: read ``content=`` -> read masters -> (optional)
    run mlox for a curated priority -> master-valid topological sort -> optionally write
    back (to ``cfg_path`` and each path in ``also``, e.g. a Kezyma base cfg).

    ``use_mlox`` runs mlox (found via ``mlox_exe`` / :func:`find_mlox`) as the ordering
    priority; if it's unavailable or errors, falls back to ``priority`` (a
    ``{name.lower(): rank}`` map -- e.g. a pinned base-master list) or alphabetical. The
    emitted order ALWAYS loads: the topological sort corrects any mlox master-order edge.
    ``apply=False`` previews without writing. Returns ``order``, ``warnings`` (mlox's
    conflict notes), ``mlox_used``, ``count``, ``master_violations`` (empty on success),
    ``applied``."""
    content = read_content(cfg_path)
    masters = {p: plugin_masters(data_dir, p) for p in content}
    warnings: List[str] = []
    mlox_used = False
    prio = priority
    if use_mlox:
        mx = mlox_exe or find_mlox()
        if mx:
            try:
                order, warnings = run_mlox(content, mx, workdir or os.path.dirname(mx) or ".", nodownload)
                if order:
                    seen: set = set()
                    ranked: List[str] = []
                    for o in order:
                        if o.lower() not in seen:
                            seen.add(o.lower())
                            ranked.append(o)
                    ranked += [c for c in content if c.lower() not in seen]   # unranked (e.g. .omwscripts) -> end
                    prio = {c.lower(): i for i, c in enumerate(ranked)}
                    mlox_used = True
            except Exception as e:                              # noqa: BLE001 -- fall back to non-mlox sort
                warnings.append("mlox invocation failed: %s" % e)
    final = topo_sort(content, masters, prio)
    pos = {c.lower(): i for i, c in enumerate(final)}
    viol = [(c, m) for c in content for m in masters[c]
            if m.lower() in pos and pos[m.lower()] > pos[c.lower()]]
    result = {"order": final, "warnings": warnings, "mlox_used": mlox_used,
              "count": len(final), "master_violations": viol, "applied": False}
    if apply and not viol:
        write_content(cfg_path, final)
        for a in (also or []):
            if os.path.exists(a):
                write_content(a, final)
        result["applied"] = True
    return result
