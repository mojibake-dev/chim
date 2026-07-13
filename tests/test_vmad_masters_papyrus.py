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
from esplib.utils import FormID
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
