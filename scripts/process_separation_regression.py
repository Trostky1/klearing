"""Live-process regression coverage for shell/backend separation."""

import ctypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN = REPO_ROOT / "main.py"


def check(label, condition):
    print(f"PASS: {label}" if condition else f"FAIL: {label}")
    return 0 if condition else 1


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def probe(port):
    try:
        with urlopen(f"http://127.0.0.1:{port}/__klearing_process_health",
                     timeout=0.3) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("app") == "klearing" and data.get("role") == "backend"
    except (OSError, URLError, ValueError):
        return False


def pid_alive(pid):
    if sys.platform == "win32":
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


HARNESS = r"""
import json, main, os, pathlib, sys
s = main.process_supervision.spawn_backend()
if not main._wait_for_backend(s, timeout=20):
    print('START-FAILED', s.process.poll(), flush=True)
    s.close()
    raise SystemExit(1)
ready = pathlib.Path(os.environ['KLEARING_HARNESS_READY'])
ready.write_text(json.dumps({'backend_pid': s.pid}), encoding='utf-8')
sys.stdin.readline()
s.close()
"""


def start_shell_harness(data_dir, port):
    env = os.environ.copy()
    env["KLEARING_DATA_DIR"] = str(data_dir)
    env["KLEARING_PORT"] = str(port)
    env["PYTHONUNBUFFERED"] = "1"
    ready_path = Path(data_dir) / f"harness-ready-{port}.json"
    env["KLEARING_HARNESS_READY"] = str(ready_path)
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", HARNESS], cwd=REPO_ROOT, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if ready_path.exists():
            payload = json.loads(ready_path.read_text(encoding="utf-8"))
            return process, int(payload["backend_pid"]), env, []
        if process.poll() is not None:
            break
        time.sleep(0.1)
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    output = process.stdout.read() if process.stdout else ""
    raise AssertionError("supervised backend did not become ready: " + output)


def wait_dead(pid, port, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid) and not probe(port):
            return True
        time.sleep(0.15)
    return False


def main_regression():
    failures = 0
    # Windows may hold SQLite/lock-file delete sharing for a moment after the
    # verified process exit. That transient cleanup detail is not a surviving
    # backend, so do not turn it into a lifecycle failure.
    with tempfile.TemporaryDirectory(
            prefix="klearing-process-", ignore_cleanup_errors=True) as data_dir:
        port = free_port()
        shell, backend_pid, env, _output = start_shell_harness(data_dir, port)
        failures += check("shell starts a distinct healthy backend child",
                          backend_pid != shell.pid and probe(port))

        second = subprocess.run(
            [sys.executable, str(MAIN), "--serve"], cwd=REPO_ROOT, env=env,
            text=True, capture_output=True, timeout=15,
        )
        failures += check("OS lock refuses a real concurrent backend",
                          second.returncode == 3
                          and "already running" in (second.stdout + second.stderr).lower())
        failures += check("refused second instance leaves owner healthy", probe(port))

        shell.stdin.write("close\n")
        shell.stdin.flush()
        shell.wait(timeout=15)
        failures += check("clean shell close gracefully reaps backend",
                          shell.returncode == 0 and wait_dead(backend_pid, port))

        # Reusing the same data directory proves the lock is released by the
        # OS rather than blocked by a stale marker file.
        port = free_port()
        shell, backend_pid, env, _output = start_shell_harness(data_dir, port)
        shell.kill()  # external, abnormal shell death
        shell.wait(timeout=8)
        failures += check("abnormal shell death cannot orphan backend",
                          wait_dead(backend_pid, port))

        restarted = subprocess.Popen(
            [sys.executable, str(MAIN), "--serve"], cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not probe(port):
            if restarted.poll() is not None:
                break
            time.sleep(0.2)
        released = probe(port)
        restarted.terminate()
        restarted.wait(timeout=10)
        failures += check("crash-released lock permits one replacement backend", released)

    direct_child = subprocess.run(
        [sys.executable, str(MAIN), "--backend-child"], cwd=REPO_ROOT,
        text=True, capture_output=True, timeout=10,
    )
    failures += check("private backend role refuses a direct launch",
                      direct_child.returncode == 2 and "internal" in direct_child.stdout.lower())

    frozen_serve = subprocess.run(
        [sys.executable, "-c",
         "import main; main.paths.is_frozen=lambda: True; raise SystemExit(main.main(['--serve']))"],
        cwd=REPO_ROOT, text=True, capture_output=True, timeout=10,
    )
    failures += check("packaged --serve is explicitly rejected",
                      frozen_serve.returncode == 2
                      and "development-only" in frozen_serve.stdout.lower())

    spec = (REPO_ROOT / "build" / "klearing.spec").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    failures += check("packaging defines exactly one executable",
                      spec.count("EXE(") == 1 and 'name="Klearing"' in spec)
    failures += check("docs name no-flag exe as sole packaged normal launch",
                      "only normal launch target is `Klearing.exe` with no flags" in readme)

    if failures:
        print(f"FAIL: {failures} process-separation regression check(s) failed.")
        return 1
    print("PASS: all process-separation regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_regression())
