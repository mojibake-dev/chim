"""Remote Papyrus compilation on the Skyrim SE remote host.

This module drives the Creation Kit's ``PapyrusCompiler.exe`` on a remote
Windows host over SSH (paramiko). It exists because Papyrus source can only be
compiled by Bethesda's own toolchain, which is Windows-only and lives next to
an installed Creation Kit -- so ``chim`` shells out to the VM rather than
reimplementing the compiler.

Everything here is a distillation of facts that were painful to discover.
Read the constants and :func:`compile_script` docstring before changing any
default; each one encodes a specific way the CK toolchain will otherwise fail
silently or produce a script that behaves wrong at runtime.

Hard-won facts baked in as defaults
------------------------------------

**SKSE relocates the extended ``Actor.psc``.**
    Vanilla Skyrim ships its script *sources* under ``Data\\Source\\Scripts``.
    SKSE, however, installs its extended script sources -- the ones that add
    ``Actor.GetWornForm``, ``Actor.GetEquippedObject`` and the rest of the
    SKSE-only API -- under ``Data\\Scripts\\Source`` (the two path components
    are **swapped**). If the vanilla directory is searched first, the compiler
    binds against the stock ``Actor.psc`` and every SKSE call fails to resolve
    ("``GetWornForm`` is not a function or does not exist"). The import list
    therefore includes **both** directories, with the SKSE one (
    ``Data\\Scripts\\Source``) **FIRST** so its ``Actor.psc`` wins.
    See :data:`DEFAULT_IMPORT_PATHS`.

**The flags file is ``TESV_Papyrus_Flags.flg``.**
    ``PapyrusCompiler.exe`` requires ``-flags=`` to name the flag-definitions
    file. For Skyrim SE that file is ``TESV_Papyrus_Flags.flg`` (note: *TESV*,
    the Skyrim-era name -- not ``Fallout`` or an unprefixed name). It normally
    sits in one of the source directories. See :data:`DEFAULT_FLAGS_FILE`.

**PowerShell mangles ``-flag=value`` arguments that contain spaces.**
    The host's default SSH shell is PowerShell. PowerShell's native-command
    argument parser rewrites tokens shaped like ``-import="A;B with spaces"``
    -- it strips or relocates quotes, so the compiler receives a broken
    ``-import`` and half the paths land as bogus positional arguments. The
    robust fix (and what this module does) is to **never** pass the compiler
    arguments through the interactive shell: write the full command line into a
    ``.bat`` file on the host and execute that batch file via ``cmd /c``. ``cmd``
    parses the ``-flag=value`` tokens the way ``PapyrusCompiler.exe`` expects.
"""

from __future__ import annotations

import os
import posixpath
import subprocess
from dataclasses import dataclass
from typing import Sequence

try:  # paramiko is a declared dependency; import lazily-friendly for testing.
    import paramiko
except ImportError:  # pragma: no cover - surfaced only when actually compiling
    paramiko = None  # type: ignore[assignment]


# --- CK toolchain locations (defaults for a stock Steam SE + SKSE install) ---

#: Standard Steam install root of the Skyrim SE ``Data`` folder on the host.
DEFAULT_DATA_ROOT = r"C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition\Data"

#: The Creation Kit compiler, installed alongside the CK in the game root.
DEFAULT_COMPILER = (
    r"C:\Program Files (x86)\Steam\steamapps\common"
    r"\Skyrim Special Edition\Papyrus Compiler\PapyrusCompiler.exe"
)

#: SKSE's extended script sources: ``Data\Scripts\Source`` -- the *swapped*
#: layout. Contains the SKSE ``Actor.psc`` with ``GetWornForm`` et al.
SKSE_SOURCE_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition\Data\Scripts\Source"

#: Vanilla script sources: ``Data\Source\Scripts`` -- the stock layout.
VANILLA_SOURCE_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition\Data\Source\Scripts"

#: Default import search path. **Order matters:** the SKSE directory is FIRST
#: so its extended ``Actor.psc`` shadows the vanilla one -- otherwise SKSE-only
#: calls (``GetWornForm``, ``GetEquippedObject``, ...) will not resolve.
DEFAULT_IMPORT_PATHS: tuple[str, ...] = (SKSE_SOURCE_DIR, VANILLA_SOURCE_DIR)

#: Skyrim SE flag-definitions file required by ``-flags=``. *TESV*, not FO4.
DEFAULT_FLAGS_FILE = "TESV_Papyrus_Flags.flg"

#: Where compiled ``.pex`` lands by default (loose scripts the game reads).
DEFAULT_OUTPUT_DIR = r"C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition\Data\Scripts"

#: SSH username used when ``host`` has no ``user@`` prefix (the remote host).
DEFAULT_SSH_USER = "user"

#: Temp location on the host for the generated batch file.
_REMOTE_BAT = r"C:\Windows\Temp\chim_papyrus_compile.bat"


@dataclass
class CompileResult:
    """Outcome of a remote ``PapyrusCompiler.exe`` run.

    Attributes:
        ok: True iff the compiler exited 0.
        exit_status: Process exit code from the batch file.
        stdout: Combined compiler stdout (the "0 failed" summary lives here).
        stderr: Compiler stderr, if any.
        command: The exact ``PapyrusCompiler.exe`` command line written into
            the ``.bat`` -- kept for debugging shell-quoting problems.
        output_pex: Remote path of the ``.pex`` the compiler was asked to emit.
    """

    ok: bool
    exit_status: int
    stdout: str
    stderr: str
    command: str
    output_pex: str


def _split_host(host: str) -> tuple[str, str]:
    """Return ``(user, hostname)`` from ``host``, defaulting the user."""
    if "@" in host:
        user, _, hostname = host.partition("@")
        return user, hostname
    return DEFAULT_SSH_USER, host


def _build_command(
    script_name: str,
    import_paths: Sequence[str],
    output_dir: str,
    compiler: str,
    flags_file: str,
) -> str:
    """Assemble the ``PapyrusCompiler.exe`` command line.

    The import search path is a single ``-import=`` argument whose value is the
    directories joined with ``;`` (the compiler's separator). We quote the
    value because the paths contain spaces -- this quoting is meant to be read
    by ``cmd.exe`` from inside a ``.bat`` file, NOT by PowerShell (see the
    module docstring for why that distinction is load-bearing).
    """
    # Strip a trailing ".psc" if the caller passed a filename; the compiler
    # wants the bare object/script name as its first positional argument.
    if script_name.lower().endswith(".psc"):
        script_name = script_name[:-4]

    import_value = ";".join(import_paths)
    parts = [
        f'"{compiler}"',
        f'"{script_name}"',
        f'-import="{import_value}"',
        f'-output="{output_dir}"',
        f'-flags="{flags_file}"',
    ]
    return " ".join(parts)


def _quote_cmd_value(value: str) -> str:
    """Quote a value for safe interpolation inside a batch echo line."""
    # Batch-file percent-escaping: %% is a literal %. Everything else in a
    # Windows path is fine once the whole value is wrapped in double quotes by
    # the caller (_build_command already does the wrapping).
    return value.replace("%", "%%")


def _build_bat_body(command: str) -> str:
    r"""Assemble the ``.bat`` body that runs ``command`` and forwards its code.

    Shared by the remote (SFTP-uploaded) and local (temp-file) execution paths
    so the batch file is byte-identical either way: ``@echo off`` keeps the
    echoed command out of stdout, then the compiler line, then ``exit /b`` with
    the compiler's errorlevel so the caller reads the compiler's own exit code.
    """
    return "\r\n".join(
        [
            "@echo off",
            _quote_cmd_value(command),
            "exit /b %errorlevel%",
        ]
    ) + "\r\n"


def _output_pex_path(output_dir: str, script_name: str) -> str:
    """Remote/POSIX-joined path of the ``.pex`` the compiler is asked to emit."""
    return posixpath.join(
        output_dir.replace("\\", "/"),
        (script_name[:-4] if script_name.lower().endswith(".psc") else script_name)
        + ".pex",
    )


def compile_script(
    host: str,
    script_name: str,
    import_paths: Sequence[str] | None = None,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    *,
    compiler: str = DEFAULT_COMPILER,
    flags_file: str = DEFAULT_FLAGS_FILE,
    ssh_port: int = 22,
    key_filename: str | None = None,
    timeout: float = 300.0,
) -> CompileResult:
    r"""Compile a Papyrus script on the remote host via ``PapyrusCompiler.exe``.

    Runs the Creation Kit compiler over SSH on ``host`` and returns a
    :class:`CompileResult`. The heavy lifting is done by three deliberate
    choices, each documented at the module level -- change them at your peril:

    1. **Import paths default to BOTH source dirs, SKSE first.** When
       ``import_paths`` is ``None`` (the default) the compiler searches
       :data:`SKSE_SOURCE_DIR` (``Data\Scripts\Source``) *before*
       :data:`VANILLA_SOURCE_DIR` (``Data\Source\Scripts``). The SKSE directory
       must come first so its extended ``Actor.psc`` (``GetWornForm``,
       ``GetEquippedObject``, ...) shadows the vanilla one; reverse the order
       and every SKSE call fails to resolve at compile time. Note the two
       path components are *swapped* between the two layouts -- that is not a
       typo, it is exactly how SKSE ships.

    2. **The flags file is ``TESV_Papyrus_Flags.flg``.** Passed as ``-flags=``;
       Skyrim SE's is TESV-prefixed. See :data:`DEFAULT_FLAGS_FILE`.

    3. **The command runs from a ``.bat`` via ``cmd /c``, never PowerShell
       directly.** The host's SSH shell is PowerShell, which mangles native
       ``-flag=value`` arguments that contain spaces (it rewrites the quoting,
       so ``-import="A;B with spaces"`` reaches the compiler broken). To avoid
       that, this function uploads a generated batch file to
       ``C:\Windows\Temp`` and executes it with ``cmd /c``; ``cmd`` passes the
       ``-flag=value`` tokens to the compiler intact.

    Args:
        host: SSH target. ``user@hostname`` or bare ``hostname`` (defaults the
            user to :data:`DEFAULT_SSH_USER`). Key-based auth only.
        script_name: The script/object name to compile (with or without a
            trailing ``.psc``), e.g. ``"MyQuestScript"``. The source ``.psc``
            must be reachable via ``import_paths``.
        import_paths: Ordered source-search directories. ``None`` -> the
            SKSE-first default; pass an explicit sequence to override, but keep
            the SKSE ``Data\Scripts\Source`` ahead of any vanilla dir.
        output_dir: Where the ``.pex`` is written on the host. Defaults to
            ``Data\Scripts`` (loose scripts the game loads directly).
        compiler: Path to ``PapyrusCompiler.exe`` on the host.
        flags_file: The ``-flags=`` value; ``TESV_Papyrus_Flags.flg`` for SE.
        ssh_port: SSH port on ``host``.
        key_filename: Optional explicit private-key path; otherwise the local
            SSH agent / default keys are used.
        timeout: Seconds to allow for connect and for the compile command.

    Returns:
        :class:`CompileResult` with the compiler's exit status and output.

    Raises:
        RuntimeError: if paramiko is not installed.
    """
    if paramiko is None:  # pragma: no cover
        raise RuntimeError(
            "paramiko is required for remote Papyrus compilation "
            "(pip install paramiko)"
        )

    paths = list(import_paths) if import_paths is not None else list(DEFAULT_IMPORT_PATHS)

    command = _build_command(
        script_name=script_name,
        import_paths=paths,
        output_dir=output_dir,
        compiler=compiler,
        flags_file=flags_file,
    )

    # Batch-file body (shared with the local path): @echo off keeps the echoed
    # command out of stdout, then the compiler line, then EXIT with its
    # errorlevel so the SSH channel's recv_exit_status reflects the compiler's
    # own exit code.
    bat_body = _build_bat_body(command)

    user, hostname = _split_host(host)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname,
            port=ssh_port,
            username=user,
            key_filename=key_filename,
            timeout=timeout,
            look_for_keys=True,
            allow_agent=True,
        )

        # Upload the batch file with SFTP so no shell quoting is involved in
        # getting the (space-laden) command onto the box.
        sftp = client.open_sftp()
        try:
            with sftp.open(_REMOTE_BAT, "w") as fh:
                fh.write(bat_body)
        finally:
            sftp.close()

        # Execute the batch via cmd /c. This is the whole point: cmd -- not the
        # default PowerShell shell -- parses the compiler's -flag=value tokens.
        exec_line = f'cmd /c "{_REMOTE_BAT}"'
        _, stdout, stderr = client.exec_command(exec_line, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        status = stdout.channel.recv_exit_status()
    finally:
        client.close()

    output_pex = _output_pex_path(output_dir, script_name)

    return CompileResult(
        ok=(status == 0),
        exit_status=status,
        stdout=out,
        stderr=err,
        command=command,
        output_pex=output_pex,
    )


def _local_bat_path() -> str:
    r"""Where the generated ``.bat`` is written on the local (Windows) box.

    Prefers ``%TEMP%``; falls back to ``C:\Windows\Temp``. The filename matches
    the remote convention so the two paths stay analogous.
    """
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    if temp:
        return os.path.join(temp, "chim_papyrus_compile.bat")
    return r"C:\Windows\Temp\chim_papyrus_compile.bat"


def compile_script_local(
    script_name: str,
    import_paths: Sequence[str] | None = None,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    *,
    compiler: str = DEFAULT_COMPILER,
    flags_file: str = DEFAULT_FLAGS_FILE,
    timeout: float = 300.0,
) -> CompileResult:
    r"""Compile a Papyrus script on THIS machine (chim running on the remote host).

    The local-execution twin of :func:`compile_script`: it builds the identical
    ``PapyrusCompiler.exe`` command line (:func:`_build_command`) and the
    identical ``.bat`` body (:func:`_build_bat_body`) -- the same three
    hard-won choices apply (SKSE-first import paths, ``TESV_Papyrus_Flags.flg``,
    and running through ``cmd /c`` a batch file so ``-flag=value`` tokens with
    spaces are parsed correctly). Instead of SFTP+SSH it writes the batch to a
    local temp path and runs it with ``subprocess.run(['cmd', '/c', bat])``.

    Args mirror :func:`compile_script` minus the SSH-only ones (``host``,
    ``ssh_port``, ``key_filename``). Returns a :class:`CompileResult` whose
    ``ok`` is ``returncode == 0``. Intended for a :class:`LocalWindowsHost`
    deployment; there is no CK compiler off Windows, so this only runs where one
    is installed.
    """
    paths = list(import_paths) if import_paths is not None else list(DEFAULT_IMPORT_PATHS)

    command = _build_command(
        script_name=script_name,
        import_paths=paths,
        output_dir=output_dir,
        compiler=compiler,
        flags_file=flags_file,
    )
    bat_body = _build_bat_body(command)

    bat_path = _local_bat_path()
    with open(bat_path, "w", newline="") as fh:
        fh.write(bat_body)

    proc = subprocess.run(
        ["cmd", "/c", bat_path],
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    output_pex = _output_pex_path(output_dir, script_name)

    return CompileResult(
        ok=(proc.returncode == 0),
        exit_status=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        command=command,
        output_pex=output_pex,
    )
