"""Tests for :mod:`chim.modlock` -- the roll: freeze a Data Files into modlock.json + bundle.

No real Morrowind files needed: plugins are arbitrary bytes (hashing is byte-agnostic; a
non-TES3 file just reports no masters). A vanilla dir + a canon dir exercise the key rule --
every content plugin is hashed (so the server can enforce it) but only ``bundle_dirs`` files
are shipped + manifested.
"""

import json
import os
import zipfile

import pytest

from chim import modlock as ML


def _setup(tmp_path, *, extra_content=""):
    vanilla = tmp_path / "vanilla"
    canon = tmp_path / "canon"
    (vanilla / "sub").mkdir(parents=True)
    canon.mkdir()
    (vanilla / "Morrowind.esm").write_bytes(b"vanilla-esm-bytes")
    (canon / "Canon.esp").write_bytes(b"canon-esp-bytes")
    (canon / "meshes").mkdir()
    (canon / "meshes" / "x.nif").write_bytes(b"nif-bytes")
    cfg = tmp_path / "openmw.cfg"
    cfg.write_text(
        f'data="{vanilla}"\n'
        f'data="{canon}"\n'
        "content=Morrowind.esm\n"
        "content=Canon.esp\n"
        f"{extra_content}"
        "fallback-archive=Morrowind.bsa\n"
        "groundcover=Grass.esp\n",
        encoding="utf-8",
    )
    return str(cfg), str(vanilla), str(canon)


def test_build_modlock_hashes_all_content_bundles_only_canon(tmp_path):
    cfg, vanilla, canon = _setup(tmp_path)
    ml = ML.build_modlock(cfg, bundle_dirs=[canon])
    # every content plugin hashed, in load order, incl. the vanilla esm the server enforces
    names = [c["name"] for c in ml["content"]]
    assert names == ["Morrowind.esm", "Canon.esp"]
    assert all(c["crc32"].startswith("0x") and len(c["sha256"]) == 64 for c in ml["content"])
    # but only canon files are bundled -- the vanilla esm is NOT redistributed
    paths = [f["path"] for f in ml["files"]]
    assert paths == ["Canon.esp", "meshes/x.nif"]
    assert "Morrowind.esm" not in paths
    assert ml["archives"] == ["Morrowind.bsa"] and ml["groundcover"] == ["Grass.esp"]
    assert ml["stats"]["plugins"] == 2 and ml["stats"]["file_count"] == 2


def test_build_modlock_missing_plugin_raises(tmp_path):
    cfg, vanilla, canon = _setup(tmp_path)
    os.remove(os.path.join(vanilla, "Morrowind.esm"))
    with pytest.raises(ML.ModlockError):
        ML.build_modlock(cfg, bundle_dirs=[canon])


def test_omwscripts_hard_errors_on_047(tmp_path):
    cfg, vanilla, canon = _setup(tmp_path, extra_content="content=Mod.omwscripts\n")
    (tmp_path / "canon" / "Mod.omwscripts").write_text("LUA=x.lua", encoding="utf-8")
    # default engine (TES3MP ~0.47, no Lua) -> hard error
    with pytest.raises(ML.ModlockError):
        ML.build_modlock(cfg, bundle_dirs=[canon])
    # a 0.48+ engine allows it
    ml = ML.build_modlock(cfg, bundle_dirs=[canon], engine={"openmw_base": "0.48.0"})
    assert "Mod.omwscripts" in [c["name"] for c in ml["content"]]


def test_required_data_files_derivation(tmp_path):
    cfg, vanilla, canon = _setup(tmp_path)
    ml = ML.build_modlock(cfg, bundle_dirs=[canon])
    rdf = ML.required_data_files(ml)
    assert [next(iter(e)) for e in rdf] == ["Morrowind.esm", "Canon.esp"]  # order preserved
    crcs = {next(iter(e)): next(iter(e.values())) for e in rdf}
    # the vanilla master carries every known-good edition CRC (so a friend's own copy passes);
    # a shipped mod carries exactly one (its exact bytes are in the zip)
    assert set(ML.VANILLA_CRCS["morrowind.esm"]).issubset(set(crcs["Morrowind.esm"]))
    assert len(crcs["Canon.esp"]) == 1
    bare = ML.required_data_files(ml, enforce_checksums=False)
    assert all(next(iter(e.values())) == [] for e in bare)                 # name+order only


def test_enforce_checksums_persisted_into_modlock(tmp_path):
    cfg, vanilla, canon = _setup(tmp_path)
    out = str(tmp_path / "seam")
    ML.roll(cfg, out, bundle_dirs=[canon], enforce_checksums=False)
    ml = json.loads(open(os.path.join(out, "modlock.json")).read())
    assert ml["enforce_checksums"] is False
    # required_data_files with no explicit flag honors the persisted choice (single source)
    assert all(next(iter(e.values())) == [] for e in ML.required_data_files(ml))


def test_assert_roll_sane_guards(tmp_path):
    cfg, vanilla, canon = _setup(tmp_path)
    ml = ML.build_modlock(cfg, bundle_dirs=[canon])
    dds = [vanilla, canon]
    assert ML.assert_roll_sane(ml, bundle_dirs=[canon], data_dirs=dds) is True   # happy path
    with pytest.raises(ML.ModlockError):                                          # wrong count
        ML.assert_roll_sane(ml, bundle_dirs=[canon], data_dirs=dds, expect_plugins=99)
    with pytest.raises(ML.ModlockError):                                          # cut-list hit
        ML.assert_roll_sane(ml, bundle_dirs=[canon], data_dirs=dds, forbid_substrings=["canon"])
    # a content plugin that resolves only OUTSIDE the bundle would be enforced-but-not-shipped
    (tmp_path / "vanilla" / "OutOfBundle.esp").write_bytes(b"x")
    cfg2 = tmp_path / "cfg2.cfg"
    cfg2.write_text(f'data="{vanilla}"\ndata="{canon}"\n'
                    "content=Morrowind.esm\ncontent=Canon.esp\ncontent=OutOfBundle.esp\n",
                    encoding="utf-8")
    ml2 = ML.build_modlock(str(cfg2), bundle_dirs=[canon])
    with pytest.raises(ML.ModlockError):
        ML.assert_roll_sane(ml2, bundle_dirs=[canon], data_dirs=[vanilla, canon])


def test_roll_forbid_substrings_and_zip_crc(tmp_path):
    cfg, vanilla, canon = _setup(tmp_path)
    with pytest.raises(ML.ModlockError):          # cut-list guard fires before any file is written
        ML.roll(cfg, str(tmp_path / "s"), bundle_dirs=[canon], forbid_substrings=["Canon"])
    assert not os.path.exists(tmp_path / "s")
    # a clean roll's zip reproduces the modlock CRCs (verify_zip_crc finds nothing wrong)
    out = str(tmp_path / "ok")
    ML.roll(cfg, out, bundle_dirs=[canon])
    ml = json.loads(open(os.path.join(out, "modlock.json")).read())
    assert ML.verify_zip_crc(os.path.join(out, "canon.zip"), ml) == []


def test_write_bundle_zip_reproducible(tmp_path):
    _cfg, _vanilla, canon = _setup(tmp_path)
    z1 = str(tmp_path / "a.zip")
    z2 = str(tmp_path / "b.zip")
    r1 = ML.write_bundle_zip([canon], z1)
    r2 = ML.write_bundle_zip([canon], z2)
    assert r1["sha256"] == r2["sha256"]  # deterministic order + fixed mtime
    with zipfile.ZipFile(z1) as zf:
        assert sorted(zf.namelist()) == ["Canon.esp", "meshes/x.nif"]


def test_write_bundle_zip_case_insensitive_merge(tmp_path):
    # Two bundle dirs supply the same asset at CASE-DIFFERING paths (a base mod's `meshes/X.NIF`
    # vs Familiar Faces' `Meshes/X.nif`). The target Data Files is case-insensitive, so shipping
    # both collides on unzip and lets the earlier (cut) copy win -- exactly the MacKom-heads leak.
    # Regression: dedupe case-insensitively, later dir wins, ship exactly one entry.
    d0 = tmp_path / "base"
    d1 = tmp_path / "ff"
    (d0 / "meshes" / "b").mkdir(parents=True)
    (d1 / "Meshes" / "b").mkdir(parents=True)
    (d0 / "meshes" / "b" / "Head.NIF").write_bytes(b"OLD-cut-mod")
    (d1 / "Meshes" / "b" / "Head.nif").write_bytes(b"NEW-familiar-faces")
    z = str(tmp_path / "out.zip")
    rep = ML.write_bundle_zip([str(d0), str(d1)], z)
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
        assert sum(n.lower() == "meshes/b/head.nif" for n in names) == 1   # deduped, not both
        entry = next(n for n in names if n.lower() == "meshes/b/head.nif")
        assert zf.read(entry) == b"NEW-familiar-faces"                     # later dir wins
    assert rep["file_count"] == 1


def test_roll_produces_all_artifacts_and_verifies(tmp_path):
    cfg, vanilla, canon = _setup(tmp_path)
    out = str(tmp_path / "seam")
    rep = ML.roll(cfg, out, bundle_dirs=[canon], server_address="mw.example.net:25565")
    for key in ("modlock", "required_data_files", "install_md"):
        assert os.path.exists(rep[key])
    assert os.path.exists(rep["zip"]["zip"])
    # requiredDataFiles.json is the TES3MP array-of-objects shape
    rdf = json.loads(open(os.path.join(out, "requiredDataFiles.json")).read())
    assert [next(iter(e)) for e in rdf] == ["Morrowind.esm", "Canon.esp"]
    # INSTALL.md carries the load order + the server; the download sha rides on the return, not the doc
    md = open(os.path.join(out, "INSTALL.md")).read()
    assert "Canon.esp" in md and "mw.example.net:25565" in md
    assert len(rep["zip"]["sha256"]) == 64
    # verify: byte-identical against the same canon
    ml = json.loads(open(rep["modlock"]).read())
    assert ML.verify(ml, [canon])["ok"] is True


def test_verify_detects_drift(tmp_path):
    cfg, vanilla, canon = _setup(tmp_path)
    ml = ML.build_modlock(cfg, bundle_dirs=[canon])
    # mutate a bundled file -> mismatch
    (tmp_path / "canon" / "Canon.esp").write_bytes(b"TAMPERED")
    v = ML.verify(ml, [canon])
    assert v["ok"] is False and "Canon.esp" in v["mismatched"]
    # add a stray local file -> extra (breaks byte-identity)
    (tmp_path / "canon" / "sneaky.esp").write_bytes(b"local addition")
    assert "sneaky.esp" in ML.verify(ml, [canon])["extra"]


def test_render_client_cfg_from_modlock(tmp_path):
    cfg, vanilla, canon = _setup(tmp_path)
    ml = ML.build_modlock(cfg, bundle_dirs=[canon])
    out = ML.render_client_cfg(ml, [vanilla, "C:/MW-Canon"])
    assert out.splitlines()[0].startswith("#")
    assert "content=Morrowind.esm" in out and "content=Canon.esp" in out
    assert "fallback-archive=Morrowind.bsa" in out
    # round-trips: reading the generated cfg back yields the modlock order + the data dirs
    p = tmp_path / "client.cfg"; p.write_text(out)
    assert ML.loadorder.read_content(str(p)) == [c["name"] for c in ml["content"]]
    assert ML.loadorder.read_data_dirs(str(p))[-1].rstrip("/") == "C:/MW-Canon"


def test_render_deploy_ps1():
    ps = ML.render_deploy_ps1("https://host/canon.zip", "ABCDEF0123",
                              "C:/MW-Canon", expect_present=["god.esp"])
    assert '-o $zip "https://host/canon.zip"' in ps
    assert 'if ($h -ne "abcdef0123")' in ps          # sha lower-cased
    assert 'tar.exe -xf $zip -C "C:/MW-Canon"' in ps
    assert "present god.esp" in ps
    assert ps.count("throw") == 3                     # download + sha + extract guards


def test_write_bundle_zip_extra_files(tmp_path):
    _cfg, _vanilla, canon = _setup(tmp_path)
    z = str(tmp_path / "e.zip")
    rep = ML.write_bundle_zip([canon], z, extra_files={"INSTALL.md": "hi", "openmw.cfg": "content=X\n"})
    assert rep["extra_files"] == ["INSTALL.md", "openmw.cfg"]
    assert rep["file_count"] == 2  # counts bundle files only (Canon.esp, meshes/x.nif), not the docs
    with zipfile.ZipFile(z) as zf:
        assert {"INSTALL.md", "openmw.cfg"}.issubset(set(zf.namelist()))
        assert zf.read("openmw.cfg").decode() == "content=X\n"


def test_roll_pre_rolls_config_into_zip(tmp_path):
    # The friend's complaint: an extracted zip had no config to reference. The roll must ship the
    # ready config files a friend needs INSIDE the zip -- a per-OS openmw.cfg, a server-preset
    # tes3mp-client-default.cfg, and a place-these-files INSTALL.md.
    cfg, vanilla, canon = _setup(tmp_path)
    out = str(tmp_path / "seam")
    rep = ML.roll(cfg, out, bundle_dirs=[canon], server_address="mw.example.net:25565")
    with zipfile.ZipFile(rep["zip"]["zip"]) as zf:
        names = set(zf.namelist())
        assert {"openmw.cfg", "tes3mp-client-default.cfg", "INSTALL.md"}.issubset(names)
        cfg_in_zip = zf.read("openmw.cfg").decode()
        tcd_in_zip = zf.read("tes3mp-client-default.cfg").decode()
        md_in_zip = zf.read("INSTALL.md").decode()
    # openmw.cfg: archives + content ACTIVE and in order; data= per-OS and COMMENTED (uncomment yours)
    assert "fallback-archive=Morrowind.bsa" in cfg_in_zip
    assert cfg_in_zip.index("content=Morrowind.esm") < cfg_in_zip.index("content=Canon.esp")
    assert "# ---- Windows ----" in cfg_in_zip and "# ---- Linux ----" in cfg_in_zip
    assert "#data=" in cfg_in_zip
    assert not any(l.startswith("data=") for l in cfg_in_zip.splitlines())  # no ACTIVE data=
    # tes3mp-client-default.cfg: preset to the server host (not host:port); blank password + note
    assert "destinationAddress = mw.example.net" in tcd_in_zip and "port = 25565" in tcd_in_zip
    assert "password =" in tcd_in_zip
    # INSTALL.md: place-these-files doc, names both configs, and OMITS the (circular) zip sha
    assert "openmw.cfg" in md_in_zip and "tes3mp-client-default.cfg" in md_in_zip
    assert rep["zip"]["sha256"] not in md_in_zip
    # siblings are byte-identical to the in-zip copies; the roll reports what it bundled
    assert open(rep["client_cfg"]).read() == cfg_in_zip
    assert open(rep["client_default_cfg"]).read() == tcd_in_zip
    assert rep["bundled"] == ["INSTALL.md", "openmw.cfg", "tes3mp-client-default.cfg"]


def test_render_client_default_cfg():
    tcd = ML.render_client_default_cfg("mw.example.net:25565")
    assert "[General]" in tcd and "[Master]" in tcd
    assert "destinationAddress = mw.example.net" in tcd  # host only, no port on this line
    assert "port = 25565" in tcd
    assert "password =" in tcd and "from your invite" in tcd  # blank + note by default
    # a supplied password is baked in and the note drops
    tcd2 = ML.render_client_default_cfg("h:26000", password="hunter2")
    assert "password = hunter2" in tcd2 and "from your invite" not in tcd2 and "port = 26000" in tcd2
    # a host with no port defaults to 25565
    assert "port = 25565" in ML.render_client_default_cfg("justhost")


def test_render_client_cfg_data_options(tmp_path):
    cfg, vanilla, canon = _setup(tmp_path)
    ml = ML.build_modlock(cfg, bundle_dirs=[canon])
    out = ML.render_client_cfg(ml, header="# hdr",
                               data_options=[("Windows", ["C:/MW/Data Files", "C:/Canon"]),
                                             ("Linux", ["/home/u/mw", "/home/u/canon"])])
    lines = out.splitlines()
    assert "# ---- Windows ----" in lines and "# ---- Linux ----" in lines
    assert '#data="C:/MW/Data Files"' in lines and "#data=C:/Canon" in lines  # space -> quoted
    assert not any(l.startswith("data=") for l in lines)  # per-OS commented, none active
    assert "content=Morrowind.esm" in lines and "content=Canon.esp" in lines  # load order still active
