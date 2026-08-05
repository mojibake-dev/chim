"""End-to-end tests for the client sync script ``clients/canon-sync.sh``.

Focus: phase 4a, the ``data=`` self-wiring. We stand up a tiny fake canon over a localhost
``http.server`` and run the REAL script against a throwaway canon dir + cfg, asserting it wires the
canon folder, keeps an existing base-game ``data=``, is idempotent, and warns (without inventing a
bogus line) when it can't autodetect Morrowind. The script's own repo copy is never touched (we drive
it with ``CANON=`` pointed at a temp dir, so its self-update writes into the temp dir instead).

The PowerShell mirror is exercised on Windows (Fenestrate) against the real Steam install, not here.
"""
import functools
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import threading

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "clients", "canon-sync.sh")

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("sh", "curl", "jq")),
    reason="canon-sync.sh needs sh + curl + jq")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):   # keep the test output clean
        pass


def _serve(root):
    """Serve ``root`` on an ephemeral localhost port. Return ``(base_url, cleanup)``."""
    handler = functools.partial(_QuietHandler, directory=str(root))
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def cleanup():
        httpd.shutdown()
        httpd.server_close()
    return f"http://127.0.0.1:{port}", cleanup


def _build_served(root):
    """Lay out a minimal served canon (modlock + tree + scripts); return the manifest sha."""
    files = {"a.txt": b"aaa\n", "sub/b.txt": b"bbb\n"}
    for rel, data in files.items():
        p = root / "tree" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    modlock = {
        "files": [{"path": rel, "sha256": _sha(data)} for rel, data in files.items()],
        "content": [{"name": "Morrowind.esm"}, {"name": "Canon.esp"}],
        "archives": ["Morrowind.bsa"],
        "groundcover": [],
    }
    ml = json.dumps(modlock, indent=2).encode()
    (root / "modlock.json").write_bytes(ml)
    for s in ("canon-sync.sh", "canon-sync.ps1"):   # so phase-6 self-update fetches something valid
        shutil.copy(os.path.join(REPO, "clients", s), root / s)
    return _sha(ml)


def _run(base, canon, cfg, settings, home, sha):
    env = dict(os.environ, BASE=base, CANON=str(canon), CFG=str(cfg),
               SETTINGS_CFG=str(settings), HOME=str(home), MANIFEST_SHA=sha)
    return subprocess.run(["sh", SCRIPT], env=env, capture_output=True, text=True, timeout=120)


def _active_data(cfg_text):
    return [l for l in cfg_text.splitlines() if l.strip().startswith("data=")]


def test_wires_canon_and_keeps_existing_base(tmp_path):
    """Wires the canon dir, preserves a pre-existing base-game data=, rewrites the load order, and is
    idempotent (a second run adds no duplicate and reports 'already in sync')."""
    served = tmp_path / "served"; served.mkdir()
    sha = _build_served(served)
    base, cleanup = _serve(served)
    try:
        canon = tmp_path / "canon"; canon.mkdir()
        home = tmp_path / "home"; home.mkdir()
        basedir = tmp_path / "MW" / "Data Files"; basedir.mkdir(parents=True)
        (basedir / "Morrowind.esm").write_bytes(b"esm")   # a real base -> autodetect is skipped
        cfg = tmp_path / "openmw.cfg"
        cfg.write_text(f'data="{basedir}"\ncontent=Old.esp\n')
        settings = tmp_path / "settings.cfg"

        r = _run(base, canon, cfg, settings, home, sha)
        assert r.returncode == 0, r.stderr
        text = cfg.read_text()
        assert f'data="{canon}"' in text                      # canon dir wired
        assert f'data="{basedir}"' in text                    # existing base kept
        assert "couldn't find your Morrowind" not in r.stderr  # base found -> no warning
        assert "wired canon data dir" in r.stdout
        assert "content=Morrowind.esm" in text and "content=Canon.esp" in text
        assert "content=Old.esp" not in text                  # load order rewritten to the manifest

        r2 = _run(base, canon, cfg, settings, home, sha)      # idempotent
        assert r2.returncode == 0, r2.stderr
        text2 = cfg.read_text()
        assert _active_data(text2).count(f'data="{canon}"') == 1
        assert "load order already in sync" in r2.stdout
        assert "wired canon data dir" not in r2.stdout        # nothing added the second time
    finally:
        cleanup()


def test_cold_seed_warns_when_morrowind_not_found(tmp_path):
    """Cold client (no cfg): seeds from the shipped openmw.cfg, wires the canon dir, and when it can't
    autodetect Morrowind it warns without inventing a bogus base data= line."""
    served = tmp_path / "served"; served.mkdir()
    sha = _build_served(served)
    base, cleanup = _serve(served)
    try:
        canon = tmp_path / "canon"; canon.mkdir()
        (canon / "openmw.cfg").write_text(   # the seed shipped in the unzip (only commented #data=)
            "# header\n#data=/example/Data Files\n#data=/example/Canon\ncontent=Seed.esp\n")
        home = tmp_path / "home"; home.mkdir()          # empty HOME -> no Morrowind anywhere
        cfg = tmp_path / "config" / "openmw.cfg"        # does not exist -> cold seed path
        settings = tmp_path / "settings.cfg"

        r = _run(base, canon, cfg, settings, home, sha)
        assert r.returncode == 0, r.stderr
        text = cfg.read_text()
        assert _active_data(text) == [f'data="{canon}"']       # only the canon dir is active
        assert "couldn't find your Morrowind" in r.stderr       # autodetect missed -> warned
        assert "#data=/example/Data Files" in text              # seed's commented examples preserved
    finally:
        cleanup()
