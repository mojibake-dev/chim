"""Tests for the additive chim primitives: VMAD codec, master-list edits, and
local Papyrus compilation.

These cover the three changes layered on top of the esplib-backed engine:

  * ``ops.vmad_to_dict`` / ``ops.vmad_from_dict`` -- a faithful, JSON-able VMAD
    codec that must round-trip **byte-identical** for scripts + Object/scalar
    properties + QUST alias-scripts (proven on a synthesized ACTI-with-object-
    property VMAD and a QUST-with-alias-script VMAD, built with esplib), plus the
    ``esp_get_vmad`` / ``esp_set_vmad`` MCP tools driven through a real
    ``safety.transaction`` on a LocalHost temp copy.
  * ``ops.add_master`` / ``ops.rename_master`` -- header master-list primitives
    that must reach the *serialized* bytes (esplib's ``add_master`` alone does
    not, on a loaded plugin) and must never renumber existing formIDs.
  * ``papyrus.compile_script_local`` -- the local-execution twin of the remote
    compiler driver; it must build the identical command + ``.bat`` body and
    shell ``cmd /c`` the batch (verified by monkeypatching ``subprocess.run``).
"""

from __future__ import annotations

import os
import shutil
import struct

import pytest

from chim.esp import ops, papyrus, safety
from chim.esp.records import walk_clean
from chim import server

from esplib.record import Record
from esplib.utils import FormID, compress_zlib
from esplib.vmad import (
    VmadData, VmadScript, VmadProperty, VmadObject, VmadAliasScripts,
    VmadFragmentData, VmadQuestFragment,
    PROP_OBJECT, PROP_INT32, PROP_STRING, PROP_FLOAT, PROP_BOOL,
    PROP_STRUCT, PROP_OBJECT_ARRAY, PROP_FLOAT_ARRAY,
)


# formIDs baked into sample.esp (mirrors test_esplib_backend.py).
FID_STAT = 0x00000800
FID_CELL = 0x00000801
FID_REFR = 0x00000802


# --------------------------------------------------------------------------- #
# VMAD codec -- byte-identical round-trip (ops level)
# --------------------------------------------------------------------------- #

def _make_acti_vmad() -> Record:
    """An ACTI carrying a VMAD with an object property + one of every scalar,
    an object array, a float array, and a nested STRUCT (with a nested object).
    """
    v = VmadData(version=5, obj_format=2)
    scr = VmadScript(name="ActiScript", flags=0)
    scr.properties.append(
        VmadProperty("Door", PROP_OBJECT, 1, VmadObject(0x00012345, -1, 0)))
    scr.properties.append(VmadProperty("Count", PROP_INT32, 1, 7))
    scr.properties.append(VmadProperty("Label", PROP_STRING, 1, "hello"))
    scr.properties.append(VmadProperty("Rate", PROP_FLOAT, 1, 1.5))
    scr.properties.append(VmadProperty("On", PROP_BOOL, 1, True))
    scr.properties.append(
        VmadProperty("Floats", PROP_FLOAT_ARRAY, 1, [1.0, 2.0, 3.0]))
    scr.properties.append(VmadProperty(
        "Doors", PROP_OBJECT_ARRAY, 1,
        [VmadObject(0xAA, -1, 0), VmadObject(0xBB, 2, 0)]))
    scr.properties.append(VmadProperty("Blob", PROP_STRUCT, 1, [
        VmadProperty("X", PROP_INT32, 1, 5),
        VmadProperty("Y", PROP_OBJECT, 1, VmadObject(0xCC, -1, 0)),
    ]))
    v.scripts.append(scr)
    rec = Record("ACTI", FormID(0x900))
    rec.add_subrecord("VMAD", v.to_bytes("ACTI"))
    return rec


def _make_qust_vmad() -> Record:
    """A QUST carrying a VMAD with a main script, a real quest fragment, and an
    alias script that itself carries an Object property.
    """
    v = VmadData(version=5, obj_format=2)
    v.scripts.append(VmadScript(name="QF_MyQuest_00012345", flags=0))
    fd = VmadFragmentData(extra_bind_version=2, filename="QF_MyQuest",
                          fragment_count=1)
    frag = VmadQuestFragment()
    frag.quest_stage = 10
    frag.unknown2 = 0
    frag.quest_stage_index = 1
    frag.unknown = 0
    frag.script_name = "QF_MyQuest"
    frag.fragment_name = "Fragment_0"
    fd.fragments.append(frag)
    v.fragment_data = fd
    alias = VmadAliasScripts(alias_obj=VmadObject(0, 0, 0), version=5,
                             obj_format=2)
    ascr = VmadScript(name="AliasScript", flags=0)
    ascr.properties.append(
        VmadProperty("Target", PROP_OBJECT, 1, VmadObject(0x00099887, -1, 0)))
    alias.scripts.append(ascr)
    v.alias_scripts.append(alias)
    rec = Record("QUST", FormID(0x901))
    rec.add_subrecord("VMAD", v.to_bytes("QUST"))
    return rec


def test_vmad_roundtrip_acti_object_property():
    rec = _make_acti_vmad()
    original = rec.get_subrecord("VMAD").data
    d = ops.vmad_to_dict(rec)
    # Object property encoded as {form_id hex, alias, unused}.
    door = d["scripts"][0]["properties"][0]
    assert door["type_name"] == "object"
    assert door["value"]["form_id"] == "0x00012345"
    # STRUCT is a nested property list; nested object stays hex.
    blob = d["scripts"][0]["properties"][-1]
    assert blob["type_name"] == "struct"
    assert blob["value"][1]["value"]["form_id"] == "0x000000CC"
    ops.vmad_from_dict(rec, d)
    assert rec.get_subrecord("VMAD").data == original


def test_vmad_roundtrip_qust_alias_scripts():
    rec = _make_qust_vmad()
    original = rec.get_subrecord("VMAD").data
    d = ops.vmad_to_dict(rec)
    assert d["fragment_data"] is not None
    assert len(d["alias_scripts"]) == 1
    assert d["alias_scripts"][0]["scripts"][0]["properties"][0]["value"][
        "form_id"] == "0x00099887"
    ops.vmad_from_dict(rec, d)
    assert rec.get_subrecord("VMAD").data == original


def test_vmad_from_dict_synthesizes_fragment_for_aliases():
    """Alias scripts present with a NULL fragment_data must still serialize --
    vmad_from_dict synthesizes an empty fragment block so the aliases survive."""
    rec = _make_qust_vmad()
    d = ops.vmad_to_dict(rec)
    # Blank the fragment body but keep the alias scripts, and drop the fragment.
    d["fragment_data"] = None
    d["scripts"] = [{"name": "QF_X", "flags": 0, "properties": []}]
    ops.vmad_from_dict(rec, d)
    reparsed = VmadData.from_record(rec)
    assert len(reparsed.alias_scripts) == 1  # aliases did NOT silently drop
    assert reparsed.fragment_data is not None


def test_vmad_to_dict_none_when_absent():
    rec = Record("ACTI", FormID(0x902))
    assert ops.vmad_to_dict(rec) is None


def test_vmad_from_dict_creates_subrecord_when_absent():
    """vmad_from_dict creates the VMAD subrecord on a record that has none."""
    rec = Record("ACTI", FormID(0x903))
    assert rec.get_subrecord("VMAD") is None
    src = _make_acti_vmad()
    ops.vmad_from_dict(rec, ops.vmad_to_dict(src))
    assert rec.get_subrecord("VMAD") is not None
    assert rec.get_subrecord("VMAD").data == src.get_subrecord("VMAD").data


# --------------------------------------------------------------------------- #
# Masters -- reach serialized bytes, no formid renumber
# --------------------------------------------------------------------------- #

def _formids(plug):
    return sorted(r.form_id.value for r in ops._iter_records(plug))


def test_add_master_reaches_serialized_bytes(raw):
    plug = ops.load(raw)
    before = _formids(plug)
    result = ops.add_master(plug, "Skyrim.esm", size=1234)
    assert result == ["Skyrim.esm"]

    out = ops.serialize(plug)
    assert walk_clean(out)
    re = ops.load(out)
    # Present in header.masters AND emitted as a MAST subrecord.
    assert re.header.masters == ["Skyrim.esm"]
    mast = [sr.get_string() for sr in re.header._raw_record.subrecords
            if sr.signature == "MAST"]
    assert mast == ["Skyrim.esm"]
    # No pre-existing formid changed.
    assert _formids(re) == before
    assert ops.find_record(re, FID_STAT) is not None


def test_add_master_idempotent_case_insensitive(raw):
    plug = ops.load(raw)
    ops.add_master(plug, "Skyrim.esm")
    again = ops.add_master(plug, "skyrim.esm")  # case-insensitive no-op
    assert again == ["Skyrim.esm"]
    # Only one MAST emitted.
    out = ops.serialize(plug)
    mast = [sr.get_string() for sr in ops.load(out).header._raw_record.subrecords
            if sr.signature == "MAST"]
    assert mast == ["Skyrim.esm"]


def test_rename_master(raw):
    plug = ops.load(raw)
    ops.add_master(plug, "Skyrim.esm")
    out = ops.serialize(plug)
    before = _formids(ops.load(out))

    plug2 = ops.load(out)
    result = ops.rename_master(plug2, "Skyrim.esm", "Update.esm")
    assert result == ["Update.esm"]
    out2 = ops.serialize(plug2)
    assert walk_clean(out2)
    re = ops.load(out2)
    assert re.header.masters == ["Update.esm"]
    mast = [sr.get_string() for sr in re.header._raw_record.subrecords
            if sr.signature == "MAST"]
    assert mast == ["Update.esm"]  # old gone, new present
    assert _formids(re) == before  # formids unchanged


def test_rename_master_missing_raises(raw):
    plug = ops.load(raw)
    with pytest.raises(ops.OpsError):
        ops.rename_master(plug, "NotAMaster.esm", "X.esm")


# --------------------------------------------------------------------------- #
# Server tools -- VMAD + masters through safety.transaction on a LocalHost
# --------------------------------------------------------------------------- #

@pytest.fixture()
def live_plugin(tmp_path, fixture_path):
    dst = os.path.join(str(tmp_path), "TestMod.esp")
    shutil.copyfile(fixture_path, dst)
    return safety.LocalHost(), dst


def _patch_host(monkeypatch, host):
    monkeypatch.setattr(server, "_make_host", lambda: host)


def test_tool_get_set_vmad_roundtrip(monkeypatch, live_plugin):
    host, path = live_plugin
    _patch_host(monkeypatch, host)

    # Initially the STAT carries no VMAD.
    got = server.esp_get_vmad(path, hex(FID_STAT))
    assert got["sig"] == "STAT"
    assert got["vmad"] is None

    # Author a VMAD onto it from the get-shaped dict of a synthesized ACTI VMAD.
    vmad_json = ops.vmad_to_dict(_make_acti_vmad())
    res = server.esp_set_vmad(path, hex(FID_STAT), vmad_json)
    assert res["form_id"] == "0x00000800"
    assert res["backup"].endswith(".bak")

    with open(path, "rb") as fh:
        data = fh.read()
    assert walk_clean(data)

    # Read it back through the tool; it must equal what we wrote (round-trip).
    got2 = server.esp_get_vmad(path, hex(FID_STAT))
    assert got2["vmad"] == vmad_json


def test_tool_get_vmad_missing_record_raises(monkeypatch, live_plugin):
    host, path = live_plugin
    _patch_host(monkeypatch, host)
    with pytest.raises(ValueError):
        server.esp_get_vmad(path, "0x00DEAD99")


def test_tool_add_master_end_to_end(monkeypatch, live_plugin):
    host, path = live_plugin
    _patch_host(monkeypatch, host)
    res = server.esp_add_master(path, "Skyrim.esm")
    assert res["masters"] == ["Skyrim.esm"]
    assert res["backup"].endswith(".bak")
    with open(path, "rb") as fh:
        data = fh.read()
    assert walk_clean(data)
    re = ops.load(data)
    assert re.header.masters == ["Skyrim.esm"]
    assert ops.find_record(re, FID_STAT) is not None  # formid preserved


def test_tool_rename_master_end_to_end(monkeypatch, live_plugin):
    host, path = live_plugin
    _patch_host(monkeypatch, host)
    server.esp_add_master(path, "Skyrim.esm")
    res = server.esp_rename_master(path, "Skyrim.esm", "Update.esm")
    assert res["masters"] == ["Update.esm"]
    with open(path, "rb") as fh:
        data = fh.read()
    assert walk_clean(data)
    assert ops.load(data).header.masters == ["Update.esm"]


def test_retired_weapon_racks_tool_is_gone():
    """The domain-specific tool + its helper/mapping were retired in favour of
    the general esp_insert_ref + esp_insert_subrecord primitives."""
    assert not hasattr(server, "esp_wire_weapon_racks")
    assert not hasattr(server, "_wire_racks_in_tree")
    assert not hasattr(server, "_WEAPON_RACK_ACTIVATOR_BASE")


# --------------------------------------------------------------------------- #
# Papyrus local-compile mode
# --------------------------------------------------------------------------- #

def test_build_command_shape():
    cmd = papyrus._build_command(
        script_name="MyScript.psc",
        import_paths=[papyrus.SKSE_SOURCE_DIR, papyrus.VANILLA_SOURCE_DIR],
        output_dir=papyrus.DEFAULT_OUTPUT_DIR,
        compiler=papyrus.DEFAULT_COMPILER,
        flags_file=papyrus.DEFAULT_FLAGS_FILE,
    )
    # The .psc suffix is stripped; SKSE dir is first in the -import value.
    assert '"MyScript"' in cmd
    assert "MyScript.psc" not in cmd
    assert f'-import="{papyrus.SKSE_SOURCE_DIR};{papyrus.VANILLA_SOURCE_DIR}"' in cmd
    assert f'-flags="{papyrus.DEFAULT_FLAGS_FILE}"' in cmd
    assert cmd.startswith(f'"{papyrus.DEFAULT_COMPILER}"')


def test_bat_body_shared_between_local_and_remote():
    """The batch body is assembled by one shared helper (identical either way)."""
    cmd = papyrus._build_command(
        "S", [papyrus.SKSE_SOURCE_DIR], papyrus.DEFAULT_OUTPUT_DIR,
        papyrus.DEFAULT_COMPILER, papyrus.DEFAULT_FLAGS_FILE,
    )
    body = papyrus._build_bat_body(cmd)
    assert body.startswith("@echo off\r\n")
    assert body.strip().endswith("exit /b %errorlevel%")
    assert cmd.replace("%", "%%") in body


def test_compile_script_local_shells_cmd_c_the_bat(monkeypatch, tmp_path):
    """compile_script_local writes the bat locally and runs it via cmd /c."""
    bat_target = os.path.join(str(tmp_path), "chim_papyrus_compile.bat")
    monkeypatch.setattr(papyrus, "_local_bat_path", lambda: bat_target)

    captured = {}

    class _Proc:
        returncode = 0
        stdout = "0 error(s)\n"
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        # The bat must already be written by the time we're invoked.
        with open(argv[-1], "r") as fh:
            captured["bat_body"] = fh.read()
        return _Proc()

    monkeypatch.setattr(papyrus.subprocess, "run", _fake_run)

    result = papyrus.compile_script_local(
        "MyScript.psc",
        output_dir=r"C:\out",
    )

    # Shelled `cmd /c <bat_path>`.
    assert captured["argv"][:2] == ["cmd", "/c"]
    assert captured["argv"][2] == bat_target
    assert captured["kwargs"].get("capture_output") is True
    assert captured["kwargs"].get("text") is True
    # The bat body carries the compiler command line.
    assert '"MyScript"' in captured["bat_body"]
    assert "@echo off" in captured["bat_body"]
    # CompileResult shape.
    assert result.ok is True
    assert result.exit_status == 0
    assert result.stdout == "0 error(s)\n"
    assert result.output_pex == "C:/out/MyScript.pex"


def test_compile_script_local_reports_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(papyrus, "_local_bat_path",
                        lambda: os.path.join(str(tmp_path), "c.bat"))

    class _Proc:
        returncode = 1
        stdout = "1 error(s)\n"
        stderr = "boom"

    monkeypatch.setattr(papyrus.subprocess, "run", lambda *a, **k: _Proc())
    result = papyrus.compile_script_local("Broken")
    assert result.ok is False
    assert result.exit_status == 1
    assert result.stderr == "boom"


def test_tool_papyrus_compile_local_path(monkeypatch):
    """With no CHIM_SSH_HOST but a Windows host, the tool uses the local compiler."""
    monkeypatch.delenv(server.ENV_SSH_HOST, raising=False)
    _patch_host(monkeypatch, safety.LocalWindowsHost())

    calls = {}

    def _fake_local(script_name, **kwargs):
        calls["script"] = script_name
        calls["kwargs"] = kwargs
        return papyrus.CompileResult(
            ok=True, exit_status=0, stdout="ok", stderr="",
            command="CMD", output_pex="C:/x/S.pex")

    def _boom_remote(*a, **k):  # must NOT be called on the local path
        raise AssertionError("remote compile_script called on local path")

    monkeypatch.setattr(papyrus, "compile_script_local", _fake_local)
    monkeypatch.setattr(papyrus, "compile_script", _boom_remote)

    out = server.papyrus_compile("MyScript")
    assert calls["script"] == "MyScript"
    assert out["ok"] is True
    assert out["output_pex"] == "C:/x/S.pex"
    assert out["command"] == "CMD"


def test_tool_papyrus_compile_remote_path(monkeypatch):
    """With CHIM_SSH_HOST set, the tool uses the remote compiler."""
    monkeypatch.setenv(server.ENV_SSH_HOST, "user@host")

    calls = {}

    def _fake_remote(host, script_name, **kwargs):
        calls["host"] = host
        calls["script"] = script_name
        return papyrus.CompileResult(
            ok=True, exit_status=0, stdout="ok", stderr="",
            command="CMD", output_pex="C:/x/S.pex")

    def _boom_local(*a, **k):
        raise AssertionError("local compile called on remote path")

    monkeypatch.setattr(papyrus, "compile_script", _fake_remote)
    monkeypatch.setattr(papyrus, "compile_script_local", _boom_local)

    out = server.papyrus_compile("MyScript")
    assert calls["host"] == "user@host"
    assert calls["script"] == "MyScript"
    assert out["ok"] is True


def test_tool_papyrus_compile_no_host_raises(monkeypatch):
    """No CHIM_SSH_HOST and a non-Windows host -> clear error (no compiler)."""
    monkeypatch.delenv(server.ENV_SSH_HOST, raising=False)
    _patch_host(monkeypatch, safety.LocalHost())
    with pytest.raises(ValueError):
        server.papyrus_compile("MyScript")


# --------------------------------------------------------------------------- #
# delete_master -- cascade FormID renumber (the correctness-critical one)
# --------------------------------------------------------------------------- #
#
# sample.esp has no masters (its own records use plugin index 0x00), so it is
# useless for exercising a cross-master renumber. We synthesize small but real
# multi-master plugins here, byte by byte, the same way fixtures/make_fixture.py
# does. Each walks clean (parse -> serialize byte-identical) before we touch it.

_TS_VC = 0x00000000
_IVERSION = 0x002C
_UNK16 = 0x0000
_FLAG_COMPRESSED = 0x00040000


def _sub(sig: bytes, payload: bytes) -> bytes:
    return sig + struct.pack("<H", len(payload)) + payload


def _rec(sig: bytes, fid: int, payload: bytes, flags: int = 0) -> bytes:
    header = sig + struct.pack(
        "<IIIIHH", len(payload), flags, fid, _TS_VC, _IVERSION, _UNK16)
    return header + payload


def _grp(label: bytes, group_type: int, children: bytes) -> bytes:
    header = b"GRUP" + struct.pack(
        "<I4siIH", 24 + len(children), label, group_type, _TS_VC, _UNK16
    ) + struct.pack("<H", 0)
    return header + children


def _tes4(masters, num_records, next_obj=0x800) -> bytes:
    """A TES4 header carrying the given master names (each with a paired DATA).

    ``num_records`` (HEDR) = count of records + groups EXCLUDING the TES4 header;
    esplib recomputes it on serialize, so it must match the tree for the raw
    fixture to round-trip byte-identical.
    """
    hedr = struct.pack("<fiI", 1.71, num_records, next_obj)
    data = _sub(b"HEDR", hedr) + _sub(b"CNAM", b"chim\x00")
    for m in masters:
        data += _sub(b"MAST", m.encode("cp1252") + b"\x00")
        data += _sub(b"DATA", struct.pack("<Q", 0))
    return _rec(b"TES4", 0x00000000, data)


def _fid(master_idx: int, obj: int) -> int:
    return ((master_idx & 0xFF) << 24) | (obj & 0x00FFFFFF)


def _flst(fid: int, lnam_fids, compressed: bool = False) -> bytes:
    """An FLST record: EDID + one LNAM subrecord per form (the tes5 schema models
    each LNAM as its own single-FormID subrecord, so this exercises the whole
    form list). Optionally emitted as a compressed record."""
    subs = _sub(b"EDID", b"TestList\x00")
    for lf in lnam_fids:
        subs += _sub(b"LNAM", struct.pack("<I", lf))
    if compressed:
        payload = struct.pack("<I", len(subs)) + compress_zlib(subs)
        return _rec(b"FLST", fid, payload, flags=_FLAG_COMPRESSED)
    return _rec(b"FLST", fid, subs)


def test_delete_master_identity_add_then_delete_end():
    """IDENTITY: on a plugin with real masters, add a dummy at the top of the
    master list, then delete it -> byte-identical to the original. Adding at the
    end lands the dummy at the highest master index; deleting it decrements
    nothing (no ref has a higher index), so the bytes must be restored exactly."""
    # Two masters A(0), B(1); self-index 2. One FLST that is an OVERRIDE of A
    # (self high byte 0) referencing A(0) and B(1) -- no ref sits at the dummy's
    # future index (2), so the add is a clean append.
    flst = _flst(_fid(0, 0x0500), [_fid(0, 0x0A00), _fid(1, 0x0B00)])
    raw = _tes4(["A.esm", "B.esm"], 2) + _grp(b"FLST", 0, flst)

    plug = ops.load(raw)
    assert ops.serialize(plug) == raw  # fixture walks clean

    ops.add_master(plug, "Dummy.esm")
    assert plug.header.masters == ["A.esm", "B.esm", "Dummy.esm"]
    after_add = ops.serialize(plug)

    plug2 = ops.load(after_add)
    masters = ops.delete_master(plug2, "Dummy.esm")
    assert masters == ["A.esm", "B.esm"]
    out = ops.serialize(plug2)
    assert walk_clean(out)
    assert out == raw  # byte-exact inverse of add_master


def test_delete_master_renumbers_mid_list():
    """RENUMBER: deleting a MID-list master decrements every higher index by one
    (refs to lower masters and the self-index shift correctly), and leaves the
    lower-index ref untouched."""
    # Three masters A(0), B(1), C(2); self-index 3. One self-owned FLST
    # (form_id at index 3) whose LNAM forms reference B(1), C(2) and itself(3).
    # There is NO ref to A(0), so deleting A is allowed.
    self_fid = _fid(3, 0x0500)
    flst = _flst(self_fid, [_fid(1, 0x0B00), _fid(2, 0x0C00), self_fid])
    raw = _tes4(["A.esm", "B.esm", "C.esm"], 2) + _grp(b"FLST", 0, flst)

    plug = ops.load(raw)
    assert ops.serialize(plug) == raw

    masters = ops.delete_master(plug, "A.esm")  # remove index 0
    assert masters == ["B.esm", "C.esm"]
    out = ops.serialize(plug)
    assert walk_clean(out)

    re = ops.load(out)
    # self-index 3 -> 2
    rec = re.get_record_by_form_id(_fid(2, 0x0500))
    assert rec is not None
    assert rec.form_id.value == _fid(2, 0x0500)
    lnam = [sr for sr in rec.subrecords if sr.signature == "LNAM"]
    vals = [struct.unpack("<I", sr.data)[0] for sr in lnam]
    # B(1)->0, C(2)->1, self(3)->2
    assert vals == [_fid(0, 0x0B00), _fid(1, 0x0C00), _fid(2, 0x0500)]
    # And the deleted master is gone from the header MAST subrecords.
    mast = [sr.get_string() for sr in re.header._raw_record.subrecords
            if sr.signature == "MAST"]
    assert mast == ["B.esm", "C.esm"]


def test_delete_master_preserves_lower_index_refs():
    """A ref to a master BELOW the deleted index must not move; only >-index refs
    decrement. Deleting the last master (index 2 of 3) touches only self-refs."""
    # A(0), B(1), C(2); self 3. FLST refs A(0) and B(1) and self(3); delete C(2).
    self_fid = _fid(3, 0x0700)
    flst = _flst(self_fid, [_fid(0, 0x0A00), _fid(1, 0x0B00), self_fid])
    raw = _tes4(["A.esm", "B.esm", "C.esm"], 2) + _grp(b"FLST", 0, flst)

    plug = ops.load(raw)
    masters = ops.delete_master(plug, "C.esm")  # highest master index
    assert masters == ["A.esm", "B.esm"]
    re = ops.load(ops.serialize(plug))
    rec = re.get_record_by_form_id(_fid(2, 0x0700))  # self 3 -> 2
    vals = [struct.unpack("<I", sr.data)[0]
            for sr in rec.subrecords if sr.signature == "LNAM"]
    # A(0) unchanged, B(1) unchanged, self(3)->2
    assert vals == [_fid(0, 0x0A00), _fid(1, 0x0B00), _fid(2, 0x0700)]


def test_delete_master_renumbers_compressed_record():
    """A compressed record's embedded FormIDs renumber too (esplib decompresses
    on load, so the walk sees them; the record recompresses on serialize)."""
    self_fid = _fid(3, 0x0500)
    flst = _flst(self_fid, [_fid(2, 0x0C00)], compressed=True)
    raw = _tes4(["A.esm", "B.esm", "C.esm"], 2) + _grp(b"FLST", 0, flst)

    plug = ops.load(raw)
    assert ops.serialize(plug) == raw
    ops.delete_master(plug, "A.esm")  # C(2)->1, self(3)->2
    re = ops.load(ops.serialize(plug))
    rec = re.get_record_by_form_id(_fid(2, 0x0500))
    assert rec.is_compressed  # stayed compressed
    lnam = [sr for sr in rec.subrecords if sr.signature == "LNAM"][0]
    assert struct.unpack("<I", lnam.data)[0] == _fid(1, 0x0C00)


def test_delete_master_renumbers_vmad_and_placement():
    """VMAD Papyrus FormIDs AND placement subrecords (NAME base, XLKR links) --
    on record types esplib does NOT schema-register -- renumber via the curated
    fallback table + the VMAD walk."""
    # ACTI (unregistered) with a VMAD object property -> C(2).
    v = VmadData(version=5, obj_format=2)
    scr = VmadScript(name="S", flags=0)
    scr.properties.append(
        VmadProperty("Door", PROP_OBJECT, 1, VmadObject(_fid(2, 0x0C00), -1, 0)))
    v.scripts.append(scr)
    acti = _rec(b"ACTI", _fid(3, 0x0500),
                _sub(b"EDID", b"A\x00") + _sub(b"VMAD", v.to_bytes("ACTI")))
    # REFR (unregistered) placing a base from C via NAME, XLKR keyword B + ref C.
    refr = _rec(b"REFR", _fid(3, 0x0600),
                _sub(b"NAME", struct.pack("<I", _fid(2, 0x0D00)))
                + _sub(b"XLKR",
                       struct.pack("<II", _fid(1, 0x0E00), _fid(2, 0x0F00))))
    raw = (_tes4(["A.esm", "B.esm", "C.esm"], 4)  # 2 groups + 2 records
           + _grp(b"ACTI", 0, acti) + _grp(b"REFR", 0, refr))

    plug = ops.load(raw)
    ops.delete_master(plug, "A.esm")  # B(1)->0, C(2)->1, self(3)->2
    re = ops.load(ops.serialize(plug))

    acti2 = re.get_record_by_form_id(_fid(2, 0x0500))
    vd = VmadData.parse(acti2.get_subrecord("VMAD").data, "ACTI")
    assert vd.scripts[0].properties[0].value.form_id == _fid(1, 0x0C00)

    refr2 = re.get_record_by_form_id(_fid(2, 0x0600))
    assert struct.unpack("<I", refr2.get_subrecord("NAME").data)[0] == \
        _fid(1, 0x0D00)
    kw, ref = struct.unpack("<II", refr2.get_subrecord("XLKR").data)
    assert kw == _fid(0, 0x0E00)   # keyword B(1)->0
    assert ref == _fid(1, 0x0F00)  # ref C(2)->1


def test_delete_master_refuses_when_referenced():
    """REFUSE: deleting a master that still has a live reference raises."""
    # A(0), B(1); FLST refs B. Deleting B must refuse.
    flst = _flst(_fid(2, 0x0500), [_fid(1, 0x0B00)])
    raw = _tes4(["A.esm", "B.esm"], 2) + _grp(b"FLST", 0, flst)
    plug = ops.load(raw)
    with pytest.raises(ops.OpsError) as ei:
        ops.delete_master(plug, "B.esm")
    assert "still referenced" in str(ei.value)
    # And the plugin was not partially mutated (still two masters, ref intact).
    assert plug.header.masters == ["A.esm", "B.esm"]


def test_delete_master_missing_raises():
    """Deleting a master that is not present raises OpsError."""
    raw = _tes4(["A.esm"], 2) + _grp(b"FLST", 0, _flst(_fid(1, 0x0500), []))
    plug = ops.load(raw)
    with pytest.raises(ops.OpsError):
        ops.delete_master(plug, "NotAMaster.esm")


def test_delete_master_refuses_esl():
    """An ESL/light plugin's FE-prefixed FormID layout is not a whole-byte
    master index, so delete_master refuses it outright."""
    raw = _tes4(["A.esm"], 2) + _grp(b"FLST", 0, _flst(_fid(1, 0x0500), []))
    plug = ops.load(raw)
    plug.header.is_esl = True
    with pytest.raises(ops.OpsError):
        ops.delete_master(plug, "A.esm")


def test_delete_master_does_not_mistake_race_name_string_for_formid():
    """The curated placement/CELL FormID table is only consulted when the schema
    does NOT type a subrecord. RACE *is* schema-registered and its NAME is an
    EspString (skeleton path), not a FormID -- so a 4-byte NAME whose bytes look
    like a high-index FormID must NOT be renumbered. Guards the schema-overrides-
    curated-table invariant."""
    # RACE NAME = the 4-char string "\x02\x00\x03m" would be misread as a FormID
    # at index 3 if the curated NAME entry wrongly applied. Use a NAME string
    # whose 4 bytes decode as high byte 0x03 (== self-index here) to prove it is
    # left untouched. RACE has no masters-referencing content otherwise.
    name_bytes = b"path"  # 4 raw bytes; byte[3]='h'=0x68 -> would be index 0x68
    race = _rec(b"RACE", _fid(3, 0x0500),
                _sub(b"EDID", b"R\x00") + _sub(b"NAME", name_bytes))
    raw = _tes4(["A.esm", "B.esm", "C.esm"], 2) + _grp(b"RACE", 0, race)
    plug = ops.load(raw)
    assert ops.serialize(plug) == raw

    ops.delete_master(plug, "A.esm")  # self 3 -> 2; NAME must stay verbatim
    re = ops.load(ops.serialize(plug))
    rec = re.get_record_by_form_id(_fid(2, 0x0500))
    assert rec is not None
    # The NAME string bytes are UNCHANGED (not renumbered as a FormID).
    assert rec.get_subrecord("NAME").data == name_bytes


def test_delete_master_end_to_end_tool(monkeypatch, tmp_path):
    """The esp_delete_master MCP tool: add a dummy master through the real tool,
    then delete it, both through a safety.transaction on a LocalHost temp copy;
    the file must walk clean and be byte-restored."""
    raw = (_tes4(["A.esm", "B.esm"], 2)
           + _grp(b"FLST", 0,
                  _flst(_fid(0, 0x0500), [_fid(0, 0x0A00), _fid(1, 0x0B00)])))
    path = os.path.join(str(tmp_path), "DelMod.esp")
    with open(path, "wb") as fh:
        fh.write(raw)

    _patch_host(monkeypatch, safety.LocalHost())
    server.esp_add_master(path, "Dummy.esm")
    res = server.esp_delete_master(path, "Dummy.esm")
    assert res["masters"] == ["A.esm", "B.esm"]
    assert res["backup"].endswith(".bak")

    with open(path, "rb") as fh:
        data = fh.read()
    assert walk_clean(data)
    assert data == raw  # add + delete round-trips byte-exact


def test_delete_master_tool_refuses_referenced(monkeypatch, tmp_path):
    """The tool surfaces the refuse-on-live-reference as a ValueError (OpsError
    subclasses ValueError) and leaves the file untouched."""
    raw = (_tes4(["A.esm", "B.esm"], 2)
           + _grp(b"FLST", 0, _flst(_fid(2, 0x0500), [_fid(1, 0x0B00)])))
    path = os.path.join(str(tmp_path), "RefMod.esp")
    with open(path, "wb") as fh:
        fh.write(raw)

    _patch_host(monkeypatch, safety.LocalHost())
    with pytest.raises(ValueError):
        server.esp_delete_master(path, "B.esm")
    # File unchanged (transaction restored / never committed a bad write).
    with open(path, "rb") as fh:
        assert fh.read() == raw
