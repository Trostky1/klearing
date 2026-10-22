"""Shell/backend lifetime ownership and the backend OS single-instance lock."""

import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone

import paths


BACKEND_CHILD_FLAG = "--backend-child"
TOKEN_ENV = "KLEARING_INTERNAL_BACKEND_TOKEN"
PARENT_PID_ENV = "KLEARING_INTERNAL_PARENT_PID"
PIPE_ENV = "KLEARING_INTERNAL_SUPERVISION_PIPE"
PIPE_KIND_ENV = "KLEARING_INTERNAL_SUPERVISION_PIPE_KIND"


class BackendAlreadyRunning(RuntimeError):
    pass


class BackendSupervisionError(RuntimeError):
    pass


class BackendInstanceLock:
    """A process-lifetime OS lock; the OS releases it after any crash."""

    def __init__(self):
        self._file = None

    def acquire(self):
        lock_path = paths.data_dir() / "backend.lock"
        lock_file = open(lock_path, "a+b")
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise BackendAlreadyRunning(
                        "Klearing's backend is already running for this data folder."
                    ) from exc
            else:
                import fcntl
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise BackendAlreadyRunning(
                        "Klearing's backend is already running for this data folder."
                    ) from exc
            lock_file.seek(1)
            lock_file.truncate()
            lock_file.write(json.dumps({
                "pid": os.getpid(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            }).encode("utf-8"))
            lock_file.flush()
        except Exception:
            lock_file.close()
            raise
        self._file = lock_file
        return self

    def close(self):
        if self._file is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()


class _WindowsKillJob:
    """A Job Object whose members die whenever the owning shell disappears."""

    KILL_ON_JOB_CLOSE = 0x00002000
    EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = self.KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                handle, self.EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            raise error
        self._ctypes = ctypes
        self._kernel32 = kernel32
        self._handle = handle

    def assign(self, process):
        if not self._kernel32.AssignProcessToJobObject(
                self._handle, process._handle):  # noqa: SLF001 - subprocess Windows handle
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def close(self):
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class BackendSupervisor:
    """The shell's ownership handle for one backend child."""

    def __init__(self, process, pipe_writer, job=None):
        self.process = process
        self._pipe_writer = pipe_writer
        self._job = job
        self._closed = False

    @property
    def pid(self):
        return self.process.pid

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._pipe_writer is not None:
            try:
                os.close(self._pipe_writer)
            except OSError:
                pass
            self._pipe_writer = None
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        finally:
            if self._job is not None:
                self._job.close()
                self._job = None


def spawn_backend():
    """Start the private backend role and attach both lifetime mechanisms."""
    read_fd, write_fd = os.pipe()
    token = secrets.token_urlsafe(32)
    env = os.environ.copy()
    env[TOKEN_ENV] = token
    env[PARENT_PID_ENV] = str(os.getpid())
    env[PIPE_KIND_ENV] = "handle" if sys.platform == "win32" else "fd"
    popen_kwargs = {"env": env}

    if paths.is_frozen():
        command = [sys.executable, BACKEND_CHILD_FLAG]
    else:
        command = [sys.executable, str(paths.resource_path("main.py")), BACKEND_CHILD_FLAG]

    if sys.platform == "win32":
        import msvcrt
        os.set_inheritable(read_fd, True)
        env[PIPE_ENV] = str(msvcrt.get_osfhandle(read_fd))
        popen_kwargs["close_fds"] = False
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        env[PIPE_ENV] = str(read_fd)
        popen_kwargs["pass_fds"] = (read_fd,)

    job = _WindowsKillJob() if sys.platform == "win32" else None
    process = None
    try:
        process = subprocess.Popen(command, **popen_kwargs)
        if job is not None:
            job.assign(process)
        return BackendSupervisor(process, write_fd, job)
    except Exception as exc:
        if process is not None:
            process.kill()
            process.wait()
        if job is not None:
            job.close()
        os.close(write_fd)
        raise BackendSupervisionError(
            f"Klearing could not start its supervised backend: {exc}") from exc
    finally:
        os.close(read_fd)


def internal_child_authorized():
    """Reject direct attempts to invoke the private packaged backend role."""
    token = os.environ.get(TOKEN_ENV, "")
    parent_pid = os.environ.get(PARENT_PID_ENV, "")
    # The unguessable token and inherited pipe are created together by the
    # shell. Parent-PID equality is deliberately not an authorization input:
    # Windows launch shims can make getppid() report the shim rather than the
    # shell even though handle inheritance is correct.
    return (len(token) >= 32 and parent_pid.isdigit()
            and PIPE_ENV in os.environ)


def child_supervision_fd():
    value = int(os.environ[PIPE_ENV])
    if os.environ.get(PIPE_KIND_ENV) == "handle":
        import msvcrt
        return msvcrt.open_osfhandle(value, os.O_RDONLY)
    return value
