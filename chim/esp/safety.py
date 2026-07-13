"""Safe, atomic plugin edits against a live Skyrim SE install.

The real target ``.esp``/``.esm`` files live in a Skyrim SE ``Data`` directory on
a **remote Windows host** reached over SSH, e.g.::

    C:\\Program Files (x86)\\Steam\\steamapps\\common\\Skyrim Special Edition\\Data\\

Editing a plugin that the Creation Kit (or the game) has open is a great way to
corrupt it, and a botched write leaves an unusable plugin behind. This module
wraps every edit in guard rails:

1. **Host abstraction** -- :class:`RemoteHost` (SSH) and :class:`LocalHost`
   (local filesystem, used by tests) share one interface: ``read_file``,
   ``write_file``, ``run_powershell``.
2. :func:`lock_check` -- refuse to touch a file while the Creation Kit, the
   CKPE loader, or the game/launcher is running (``Get-Process``).
3. :func:`backup` -- copy the file to a timestamped ``.bak`` beside it first.
4. :func:`transaction` -- a context manager that ties it together: lock-check
   (raise if locked) -> backup -> hand the caller the current file **bytes** to
   edit -> write the edited bytes back -> verify the result is *walk-clean*
   (:func:`chim.esp.records.walk_clean`). If the edit left the plugin dirty the
   original is restored from the backup and the transaction raises.

Typical use::

    from chim.esp.safety import RemoteHost, transaction
    from chim import parse_plugin, serialize

    host = RemoteHost("user@host")
    data_dir = r"C:\\Program Files (x86)\\Steam\\steamapps\\common" \\
               r"\\Skyrim Special Edition\\Data"
    path = data_dir + r"\\MyMod.esp"

    with transaction(host, path) as txn:
        plugin = parse_plugin(txn.data)
        ...  # edit plugin in place
        txn.data = serialize(plugin)
    # on exit: written back to the VM, verified walk-clean, or rolled back.

``paramiko`` is only imported when a :class:`RemoteHost` actually opens an SSH
connection with the paramiko backend, so importing this module (and running the
``LocalHost`` tests) never requires it.
"""

from __future__ import annotations

import base64
import os
import posixpath
import shlex
import shutil
import subprocess
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, List, Optional

from .records import walk_clean

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Default Skyrim SE Data directory on a stock Steam Windows install.
DEFAULT_DATA_DIR = (
    r"C:\Program Files (x86)\Steam\steamapps\common"
    r"\Skyrim Special Edition\Data"
)

#: Process image names (no ``.exe``, as ``Get-Process`` reports them) that must
#: not be running while we edit a plugin. Covers the Creation Kit, the CKPE
#: (Creation Kit Platform Extended) loader, and the game + its launcher.
LOCK_PROCESSES: tuple[str, ...] = (
    "CreationKit",
    "ckpe_loader",
    "SkyrimSE",
    "SkyrimSELauncher",
)

#: Timestamp format used for ``.bak`` file names. Sorts lexicographically.
BACKUP_TIMESTAMP_FMT = "%Y%m%d-%H%M%S"


class LockError(RuntimeError):
    """Raised when a process holding a plugin open blocks an edit."""

    def __init__(self, processes: List[str]) -> None:
        self.processes = list(processes)
        joined = ", ".join(self.processes)
        super().__init__(
            f"cannot edit plugin: locking process(es) running: {joined}"
        )


class TransactionError(RuntimeError):
    """Raised when a transaction's write leaves the plugin not walk-clean.

    ``restored`` records whether the original file was successfully rolled back
    from its backup.
    """

    def __init__(self, path: str, backup_path: str, restored: bool) -> None:
        self.path = path
        self.backup_path = backup_path
        self.restored = restored
        state = "restored from backup" if restored else "RESTORE FAILED"
        super().__init__(
            f"edited plugin {path!r} is not walk-clean; {state} "
            f"(backup at {backup_path!r})"
        )


# --------------------------------------------------------------------------- #
# Host abstraction
# --------------------------------------------------------------------------- #

class Host(ABC):
    """A machine that holds plugin files and can run PowerShell.

    Concrete hosts must implement byte-exact ``read_file`` / ``write_file`` and
    a ``run_powershell`` that returns the command's stdout as text.
    """

    #: True on Windows-flavoured hosts (path separators, PowerShell semantics).
    windows: bool = True

    @abstractmethod
    def read_file(self, path: str) -> bytes:
        """Return the full contents of ``path`` as bytes."""

    @abstractmethod
    def write_file(self, path: str, data: bytes) -> None:
        """Write ``data`` to ``path``, replacing any existing file."""

    @abstractmethod
    def run_powershell(self, script: str) -> str:
        """Run a PowerShell ``script`` on the host and return its stdout."""

    # -- shared helpers --------------------------------------------------- #

    def exists(self, path: str) -> bool:
        """Return whether ``path`` exists on the host."""
        try:
            self.read_file(path)
            return True
        except FileNotFoundError:
            return False

    def copy_file(self, src: str, dst: str) -> None:
        """Copy ``src`` to ``dst`` on the host (default: read + write)."""
        self.write_file(dst, self.read_file(src))


# --------------------------------------------------------------------------- #
# RemoteHost -- the real target (Windows host over SSH)
# --------------------------------------------------------------------------- #

class RemoteHost(Host):
    """A Windows host reached over SSH.

    Two transports are supported, chosen by ``backend``:

    * ``"ssh"`` (default): shell out to the system ``ssh`` client. Nothing to
      install; honours the user's SSH config, keys and agent.
    * ``"paramiko"``: use the :mod:`paramiko` library (imported lazily). Useful
      when no ``ssh`` binary is available.

    File bytes are moved as base64 to survive PowerShell's text mangling and to
    stay binary-safe regardless of the remote code page. ``target`` is anything
    an ``ssh`` command line accepts, e.g. ``"user@host"``.
    """

    windows = True

    def __init__(
        self,
        target: str,
        *,
        backend: str = "ssh",
        port: Optional[int] = None,
        ssh_binary: str = "ssh",
        ssh_options: Optional[List[str]] = None,
        connect_kwargs: Optional[dict] = None,
        timeout: int = 120,
    ) -> None:
        if backend not in ("ssh", "paramiko"):
            raise ValueError(f"unknown backend {backend!r}")
        self.target = target
        self.backend = backend
        self.port = port
        self.ssh_binary = ssh_binary
        self.ssh_options = list(ssh_options or [])
        self.connect_kwargs = dict(connect_kwargs or {})
        self.timeout = timeout
        self._client = None  # lazily-created paramiko.SSHClient

    # -- transport -------------------------------------------------------- #

    def _run_ssh_powershell(self, script: str) -> str:
        """Run ``script`` via the system ssh client, return stdout text.

        The PowerShell script is base64-encoded (UTF-16LE) and handed to
        ``powershell -EncodedCommand`` so quoting survives the double trip
        through the local shell and the remote command line.
        """
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        remote_cmd = f"powershell -NoProfile -EncodedCommand {encoded}"
        argv = [self.ssh_binary]
        if self.port is not None:
            argv += ["-p", str(self.port)]
        argv += self.ssh_options
        argv += [self.target, remote_cmd]
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=self.timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"ssh powershell failed (rc={proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace').strip()}"
            )
        return proc.stdout.decode("utf-8", "replace")

    def _paramiko_client(self):
        """Return a connected paramiko SSHClient, creating it on first use."""
        if self._client is not None:
            return self._client
        import paramiko  # lazy: only needed for the paramiko backend

        user: Optional[str] = None
        host = self.target
        if "@" in self.target:
            user, host = self.target.split("@", 1)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(self.connect_kwargs)
        kwargs.setdefault("hostname", host)
        if user is not None:
            kwargs.setdefault("username", user)
        if self.port is not None:
            kwargs.setdefault("port", self.port)
        kwargs.setdefault("timeout", self.timeout)
        client.connect(**kwargs)
        self._client = client
        return client

    def _run_paramiko_powershell(self, script: str) -> str:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        remote_cmd = f"powershell -NoProfile -EncodedCommand {encoded}"
        client = self._paramiko_client()
        _stdin, stdout, stderr = client.exec_command(
            remote_cmd, timeout=self.timeout
        )
        out = stdout.read()
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            err = stderr.read().decode("utf-8", "replace").strip()
            raise RuntimeError(
                f"paramiko powershell failed (rc={rc}): {err}"
            )
        return out.decode("utf-8", "replace")

    def run_powershell(self, script: str) -> str:
        if self.backend == "paramiko":
            return self._run_paramiko_powershell(script)
        return self._run_ssh_powershell(script)

    def close(self) -> None:
        """Close the paramiko connection if one was opened."""
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- files (binary-safe via base64 + PowerShell) ---------------------- #

    def read_file(self, path: str) -> bytes:
        """Read ``path`` as raw bytes.

        Emits base64 from the remote and decodes locally so the transfer is
        binary-clean. Raises :class:`FileNotFoundError` if the file is absent.
        """
        script = (
            f"if (-not (Test-Path -LiteralPath {_ps_quote(path)})) "
            f"{{ Write-Error 'CHIM_NOT_FOUND'; exit 3 }}; "
            f"$b=[System.IO.File]::ReadAllBytes({_ps_quote(path)}); "
            f"[System.Convert]::ToBase64String($b)"
        )
        try:
            out = self.run_powershell(script)
        except RuntimeError as exc:
            if "CHIM_NOT_FOUND" in str(exc):
                raise FileNotFoundError(path) from exc
            raise
        b64 = "".join(out.split())
        return base64.b64decode(b64) if b64 else b""

    def write_file(self, path: str, data: bytes) -> None:
        """Write ``data`` to ``path`` byte-for-byte (base64 round-trip)."""
        b64 = base64.b64encode(data).decode("ascii")
        script = (
            f"$b=[System.Convert]::FromBase64String('{b64}'); "
            f"[System.IO.File]::WriteAllBytes({_ps_quote(path)}, $b)"
        )
        self.run_powershell(script)

    def copy_file(self, src: str, dst: str) -> None:
        """Server-side copy, preserving bytes without pulling them local."""
        script = (
            f"Copy-Item -LiteralPath {_ps_quote(src)} "
            f"-Destination {_ps_quote(dst)} -Force"
        )
        self.run_powershell(script)


# --------------------------------------------------------------------------- #
# LocalHost -- test / dev double, backed by the local filesystem
# --------------------------------------------------------------------------- #

class LocalHost(Host):
    """A host backed by the local filesystem.

    Used by the test suite (edit a temp copy of the fixture) and for dry runs
    on the developer's own machine. ``run_powershell`` is a stub that reports no
    running processes so :func:`lock_check` treats a local host as always
    unlocked -- override :attr:`fake_processes` to simulate a lock.
    """

    windows = False

    def __init__(self, fake_processes: Optional[List[str]] = None) -> None:
        #: Process names :func:`lock_check` should pretend are running.
        self.fake_processes: List[str] = list(fake_processes or [])

    def read_file(self, path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    def write_file(self, path: str, data: bytes) -> None:
        with open(path, "wb") as fh:
            fh.write(data)

    def copy_file(self, src: str, dst: str) -> None:
        shutil.copyfile(src, dst)

    def run_powershell(self, script: str) -> str:
        """Emulate just enough for :func:`lock_check`.

        A real ``Get-Process`` query returns one matched process name per line;
        here we echo whichever of :attr:`fake_processes` the query asked about.
        Any other script yields empty output.
        """
        matched = [
            name for name in self.fake_processes if name in script
        ]
        return "\n".join(matched)


# --------------------------------------------------------------------------- #
# LocalWindowsHost -- chim running ON the remote host itself
# --------------------------------------------------------------------------- #

class LocalWindowsHost(LocalHost):
    """The Windows machine chim itself runs on.

    Inherits :class:`LocalHost`'s direct filesystem I/O -- so ``write_file`` is a
    plain ``open(path, "wb")`` with no base64 / command-line size limit (unlike
    :class:`RemoteHost`, whose one-shot ``FromBase64String`` command overflows on
    a ~500KB plugin) -- but overrides ``run_powershell`` to invoke the *real*
    local PowerShell. That makes :func:`lock_check` actually observe the Creation
    Kit / game running, which the stubbed :class:`LocalHost` cannot. Use this
    host when serving chim on the remote host (local files + honest lock-check).
    """

    windows = True

    def __init__(self, *, timeout: int = 120) -> None:
        super().__init__()
        self.timeout = timeout

    def run_powershell(self, script: str) -> str:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=self.timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"local powershell failed (rc={proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace').strip()}"
            )
        return proc.stdout.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# PowerShell quoting
# --------------------------------------------------------------------------- #

def _ps_quote(value: str) -> str:
    """Return ``value`` as a single-quoted PowerShell string literal."""
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------- #
# Lock check
# --------------------------------------------------------------------------- #

def lock_check(host: Host) -> List[str]:
    """Return the names of any lock-holding processes running on ``host``.

    Queries ``Get-Process`` for each name in :data:`LOCK_PROCESSES`. An empty
    list means it is safe to edit. This never raises on "nothing running"; it
    is the caller's (or :func:`transaction`'s) job to decide what an occupied
    list means.
    """
    names = ",".join(f"'{n}'" for n in LOCK_PROCESSES)
    # Filter all processes with Where-Object rather than `Get-Process -Name`:
    # `-Name` emits a non-terminating error AND a non-zero process exit code for
    # every name with no running match, so the all-absent case -- the UNLOCKED
    # case we most need to succeed -- exits 1 and a strict run_powershell reads it
    # as a failure (verified live on the remote host). Where-Object never errors on
    # no match, so the script exits 0 whether or not anything is running. Progress
    # is silenced so first-run module prep does not spew onto stderr.
    script = (
        "$ProgressPreference='SilentlyContinue'; "
        "Get-Process | Where-Object { @(" + names + ") -contains $_.Name } "
        "| Select-Object -ExpandProperty ProcessName -Unique"
    )
    out = host.run_powershell(script)
    running: List[str] = []
    seen = set()
    for line in out.splitlines():
        name = line.strip()
        if name and name in LOCK_PROCESSES and name not in seen:
            seen.add(name)
            running.append(name)
    return running


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #

def _join(host: Host, directory: str, name: str) -> str:
    """Join a directory and filename using the host's path flavour."""
    if host.windows:
        sep = "\\" if "\\" in directory or "/" not in directory else "/"
        return directory.rstrip("\\/") + sep + name
    return posixpath.join(directory, name)


def _split(host: Host, path: str) -> tuple[str, str]:
    """Split ``path`` into (directory, filename) for the host's flavour."""
    if host.windows:
        norm = path.replace("/", "\\")
        idx = norm.rfind("\\")
        if idx == -1:
            return "", path
        return norm[:idx], norm[idx + 1 :]
    return os.path.split(path)


def backup(host: Host, path: str, *, timestamp: Optional[str] = None) -> str:
    """Copy ``path`` to a timestamped ``.bak`` beside it; return the new path.

    The backup name is ``<original>.<YYYYmmdd-HHMMSS>.bak`` so repeated backups
    never collide and sort chronologically. ``timestamp`` overrides the clock
    (used by tests for determinism).
    """
    if not host.exists(path):
        raise FileNotFoundError(path)
    ts = timestamp or datetime.now().strftime(BACKUP_TIMESTAMP_FMT)
    directory, name = _split(host, path)
    bak_name = f"{name}.{ts}.bak"
    bak_path = _join(host, directory, bak_name) if directory else bak_name
    host.copy_file(path, bak_path)
    return bak_path


# --------------------------------------------------------------------------- #
# Transaction
# --------------------------------------------------------------------------- #

@dataclass
class _Txn:
    """Mutable handle yielded by :func:`transaction`.

    ``data`` starts as the current on-disk bytes; assign the edited bytes back
    to it before the ``with`` block exits. ``backup_path`` is where the pre-edit
    copy lives (for manual recovery if you ever need it).
    """

    path: str
    data: bytes
    backup_path: str


@contextmanager
def transaction(
    host: Host,
    path: str,
    *,
    verify: bool = True,
    timestamp: Optional[str] = None,
) -> Iterator[_Txn]:
    """Safely edit the plugin at ``path`` on ``host``.

    Sequence:

    1. **Lock check.** If the Creation Kit / CKPE loader / game / launcher is
       running, raise :class:`LockError` and touch nothing.
    2. **Backup.** Copy the file to a timestamped ``.bak`` (see :func:`backup`).
    3. **Yield.** Hand back a :class:`_Txn` whose ``data`` is the current file
       bytes. Edit and reassign ``txn.data`` inside the ``with`` block.
    4. **Write back.** On clean block exit, write ``txn.data`` to ``path``.
    5. **Verify.** Unless ``verify=False``, confirm the written bytes are
       walk-clean (:func:`chim.esp.records.walk_clean`). If they are not, the
       original is restored from the backup and :class:`TransactionError` is
       raised.

    If the body raises, no write happens; the backup is left in place so the
    original is always recoverable.
    """
    locked = lock_check(host)
    if locked:
        raise LockError(locked)

    bak_path = backup(host, path, timestamp=timestamp)
    original = host.read_file(path)
    txn = _Txn(path=path, data=original, backup_path=bak_path)

    yield txn

    # Body completed without raising: commit the (possibly edited) bytes.
    host.write_file(path, txn.data)

    if not verify:
        return

    written = host.read_file(path)
    if walk_clean(written):
        return

    # Dirty result: roll back from the backup and report.
    restored = False
    try:
        host.copy_file(bak_path, path)
        restored = walk_clean(host.read_file(path))
    except Exception:  # pragma: no cover - restore best-effort
        restored = False
    raise TransactionError(path, bak_path, restored)


__all__ = [
    "DEFAULT_DATA_DIR",
    "LOCK_PROCESSES",
    "BACKUP_TIMESTAMP_FMT",
    "Host",
    "RemoteHost",
    "LocalHost",
    "LocalWindowsHost",
    "LockError",
    "TransactionError",
    "lock_check",
    "backup",
    "transaction",
]
