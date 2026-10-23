"""Child processes: hidden console, UTF-8, Win32 Job Object kill tree, progress pipe parsing.

Rules: `CREATE_NO_WINDOW`, `stdin=DEVNULL` unless the caller feeds the
process, arguments as argv items, decode with `errors="replace"`, cancel kills the whole tree through a Job Object
with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (`taskkill /T /F` only as a fallback).
"""

from __future__ import annotations

import collections
import ctypes
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import IO, Callable, Iterable

log = logging.getLogger("premiere_deck.core.procs")

CREATE_NO_WINDOW = 0x08000000
STDERR_TAIL = 200


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)  # a frozen parent must not leak its Python into child tools
    env.pop("PYTHONPATH", None)
    env["PYTHONUTF8"] = "1"
    env["AV_LOG_FORCE_NOCOLOR"] = "1"
    for key in [k for k in env if k.startswith("_PYI") or k == "_MEIPASS2"]:
        env.pop(key, None)  # a child must not reuse the one-file exe's extraction folder
    return env


CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def launch_detached(args: list[str], cwd: str | os.PathLike | None = None) -> subprocess.Popen:
    """Start a GUI program that must outlive the host (Premiere Pro): no Job Object, own process group, outside
    the host's job when the host runs inside one (CEP starts the host that way). Raises OSError."""
    base = CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  cwd=str(cwd) if cwd else None, env=child_env(), close_fds=True)
    try:
        return subprocess.Popen([str(a) for a in args], creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError:
        if os.name != "nt":
            raise
        return subprocess.Popen([str(a) for a in args], creationflags=base, **kwargs)


def format_cmd(args: Iterable[str]) -> str:
    """A copy-pasteable Windows command line (for logs and the CLI plan)."""
    return subprocess.list2cmdline([str(a) for a in args])


class JobObject:
    """Win32 Job Object with KILL_ON_JOB_CLOSE: terminating or closing it kills every assigned process tree."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _ExtLimit(ctypes.Structure):
        pass

    _ExtLimit._fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]

    def __init__(self) -> None:
        self.handle = None
        self._procs: list[subprocess.Popen] = []
        if os.name != "nt":
            return
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._k32 = k32
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = self._ExtLimit()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(handle, self.JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(handle)
            return
        self.handle = handle

    def assign(self, proc: subprocess.Popen) -> bool:
        self._procs.append(proc)
        if not self.handle:
            return False
        try:
            return bool(self._k32.AssignProcessToJobObject(self.handle, int(proc._handle)))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False

    def kill(self) -> None:
        """Kill every process of the job (and their children)."""
        if self.handle:
            self._k32.TerminateJobObject(self.handle, 1)
        for proc in self._procs:
            if proc.poll() is None:
                if os.name == "nt" and not self.handle:
                    try:
                        subprocess.run([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                                                     "taskkill.exe"), "/PID", str(proc.pid), "/T", "/F"],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=10)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    proc.kill()
                except OSError:
                    pass

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "JobObject":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def spawn(args: list[str], *, job: JobObject | None = None, stdin: int | IO | None = subprocess.DEVNULL,
          stdout: int | IO | None = subprocess.PIPE, stderr: int | IO | None = subprocess.PIPE,
          cwd: str | os.PathLike | None = None) -> subprocess.Popen:
    """Start a hidden child process and put it into `job`. Raises OSError when the executable cannot start."""
    proc = subprocess.Popen([str(a) for a in args], stdin=stdin, stdout=stdout, stderr=stderr,
                            cwd=str(cwd) if cwd else None, env=child_env(),
                            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    if job is not None:
        job.assign(proc)
    return proc


class LinePump(threading.Thread):
    """Reads a text stream line by line; keeps the last lines and forwards each to a callback."""

    def __init__(self, stream: IO[bytes], on_line: Callable[[str], None] | None = None,
                 keep: int = STDERR_TAIL) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.on_line = on_line
        self.tail: collections.deque[str] = collections.deque(maxlen=keep)
        self.start()

    def run(self) -> None:
        try:
            for raw in iter(self.stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                self.tail.append(line)
                if self.on_line is not None:
                    try:
                        self.on_line(line)
                    except Exception:  # noqa: BLE001  a parser bug must not stop the pump
                        log.exception("line callback failed")
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.stream.close()
            except OSError:
                pass

    def lines(self) -> list[str]:
        return list(self.tail)


class BytesPump(threading.Thread):
    """Collects a binary stream up to `limit` bytes."""

    def __init__(self, stream: IO[bytes], limit: int = 64 << 20) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = limit
        self.chunks: list[bytes] = []
        self.start()

    def run(self) -> None:
        size = 0
        try:
            while True:
                chunk = self.stream.read(1 << 16)
                if not chunk:
                    break
                if size < self.limit:
                    self.chunks.append(chunk)
                    size += len(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.stream.close()
            except OSError:
                pass

    def data(self) -> bytes:
        return b"".join(self.chunks)


@dataclass
class ProcResult:
    returncode: int | None
    stderr: list[str] = field(default_factory=list)
    stdout: bytes = b""
    cancelled: bool = False
    timed_out: bool = False
    start_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.cancelled and not self.timed_out

    def last_error_line(self) -> str:
        for line in reversed(self.stderr):
            text = line.strip()
            if text:
                return text
        return self.start_error or ""


def run(args: list[str], *, on_line: Callable[[str], None] | None = None, capture: bool = False,
        cancel: threading.Event | None = None, timeout: float | None = None,
        cwd: str | os.PathLike | None = None, job: JobObject | None = None) -> ProcResult:
    """Run a tool to completion. `on_line` receives stdout lines (progress pipe); `capture` keeps stdout bytes."""
    own_job = job is None
    job = job or JobObject()
    try:
        try:
            proc = spawn(args, job=job, cwd=cwd)
        except OSError as exc:
            return ProcResult(None, start_error=f"cannot start {os.path.basename(str(args[0]))}: {exc}")
        err = LinePump(proc.stderr)  # type: ignore[arg-type]
        if capture:
            out: LinePump | BytesPump = BytesPump(proc.stdout)  # type: ignore[arg-type]
        else:
            out = LinePump(proc.stdout, on_line, keep=20)  # type: ignore[arg-type]
        deadline = time.monotonic() + timeout if timeout else None
        cancelled = timed_out = False
        while True:
            try:
                proc.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and cancel.is_set():
                cancelled = True
                job.kill()
                break
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                job.kill()
                break
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        out.join(timeout=5)
        err.join(timeout=5)
        stdout = out.data() if isinstance(out, BytesPump) else b""
        return ProcResult(proc.returncode, err.lines(), stdout, cancelled, timed_out)
    finally:
        if own_job:
            job.close()


class ProgressParser:
    """Turns `-progress pipe:1` key=value lines into one dict per block (the block ends with `progress=`)."""

    def __init__(self, on_block: Callable[[dict[str, str]], None]) -> None:
        self.on_block = on_block
        self._block: dict[str, str] = {}

    def feed(self, line: str) -> None:
        key, sep, value = line.partition("=")
        if not sep:
            return
        key, value = key.strip(), value.strip()
        self._block[key] = value
        if key == "progress":
            block, self._block = self._block, {}
            self.on_block(block)


def out_time_seconds(block: dict[str, str]) -> float | None:
    """Seconds encoded so far from a progress block (`out_time_us`, or `out_time_ms`, which is also in us)."""
    for key in ("out_time_us", "out_time_ms"):
        raw = block.get(key)
        if raw and raw not in ("N/A", ""):
            try:
                value = int(raw)
            except ValueError:
                continue
            if value >= 0:
                return value / 1_000_000
    return None


def speed_factor(block: dict[str, str]) -> float | None:
    raw = block.get("speed", "").strip().rstrip("x")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None
