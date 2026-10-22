"""
Klearing's entry point — one packaged launch target with internal roles.

  Klearing.exe            open the app in its own native window
  python main.py --serve  development-only server, no window
  klearing --daily-check  run the morning check and exit
                          (what the 8:00 AM task runs)
  klearing --weekly-digest  send the digest and exit (Monday task)

The native shell starts the FastAPI server as a private child. On Windows,
the child belongs to a kill-on-close Job Object; on every platform it also
watches a parent-lifetime pipe. Closing or crashing the shell therefore
cannot leave an orphan holding the database or port. The child has a real
OS file lock as a second line of defense against concurrent backends.

Packaged builds are windowed (no console), so when there's no stdout
each command routes its output to the matching log in the data dir —
that's what the in-app Logs page shows.

Dev use: python main.py [flags].
"""

import multiprocessing
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from html import escape

import paths
import process_supervision
from config import (APP_NAME, APP_URL, APP_VERSION, GITHUB_RELEASES_URL,
                    PORT)
from migrations.schema_versions import (NewerSchemaVersionError,
                                        StorageCompatibilityError)


# Klearing is intentionally a local-only web application. Keep one constant
# for native and headless server modes so a temporary network-testing change
# cannot leave one launch path exposed at release time.
LOCAL_BIND_HOST = "127.0.0.1"


def _route_output_to(log_name):
    """Point stdout/stderr at a log file when there is no console
    (pythonw / windowed PyInstaller builds), so nothing crashes on a
    None stdout and every run leaves a readable trace."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = open(paths.log_path(log_name), "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _log_lifecycle(event):
    """Append a timestamped started/stopped line to app.log."""
    try:
        with open(paths.log_path("app"), "a", encoding="utf-8") as log_file:
            log_file.write(f"{datetime.now(timezone.utc).isoformat()} {event}\n")
    except OSError:
        pass  # logging must never block the app itself


def _storage_refusal_copy(exc):
    if isinstance(exc, NewerSchemaVersionError):
        lead = ("Klearing needs an update. These books were opened by a "
                "newer version of Klearing.")
    else:
        lead = ("Klearing couldn't safely identify this database version, "
                "so it did not open the books.")
    return (f"{lead} Visit {GITHUB_RELEASES_URL} to get the latest version. "
            "No data was changed.")


def _persist_storage_refusal(log_name, exc):
    """Leave an unattended-run diagnostic even when stdout is ephemeral."""
    line = (f"{datetime.now(timezone.utc).isoformat()} storage refused: "
            f"{type(exc).__name__}: {exc}\n")
    try:
        with open(paths.log_path(log_name), "a", encoding="utf-8") as log_file:
            log_file.write(line)
    except OSError:
        pass


def _show_native_storage_refusal(message):
    """Display a server-free update window; fall back to the update page."""
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{escape(APP_NAME)} needs an update</title>
<style>
body {{ margin: 0; background: #f7f5ef; color: #18211d;
       font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
main {{ max-width: 560px; margin: 0 auto; padding: 54px 46px; }}
h1 {{ margin: 0 0 18px; font-size: 28px; }}
p {{ margin: 0 0 22px; }}
a {{ display: inline-block; padding: 11px 16px; border-radius: 8px;
     background: #1e6752; color: white; text-decoration: none; font-weight: 600; }}
.safe {{ margin-top: 24px; color: #4d5d55; font-size: 14px; }}
</style></head><body><main>
<h1>{escape(APP_NAME)} needs an update</h1>
<p>{escape(message)}</p>
<a href="{escape(GITHUB_RELEASES_URL)}" target="_blank">Get the latest version</a>
<p class="safe">Your database was left unchanged.</p>
</main></body></html>"""
    try:
        import webview
        window = webview.create_window(
            f"{APP_NAME} needs an update", html=html,
            width=680, height=430, resizable=False,
        )
        icon = paths.resource_path("assets/klearing.ico")
        webview.start(icon=str(icon) if icon.exists() else None)
        return window
    except Exception as window_error:
        print(f"Update window unavailable ({window_error}).")
        webbrowser.open(GITHUB_RELEASES_URL)
        return None


def _handle_storage_refusal(exc, log_name, native=False):
    message = _storage_refusal_copy(exc)
    _persist_storage_refusal(log_name, exc)
    print(message)
    print(f"Technical detail: {exc}")
    if native:
        _show_native_storage_refusal(message)
    return 2


def _server_is_klearing():
    """Distinguish our backend from an unrelated service on the same port."""
    import json
    from urllib.error import URLError
    from urllib.request import urlopen

    try:
        with urlopen(
                f"http://{LOCAL_BIND_HOST}:{PORT}/__klearing_process_health",
                timeout=0.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("app") == "klearing" and payload.get("role") == "backend"
    except (OSError, URLError, ValueError):
        return False


def _catch_up_recurring_invoices():
    """Queue any recurring invoices that came due while the app was shut.

    The daily 8am task also runs this, but only if the machine was on to
    run it. Launch is the other half of the catch-up: a laptop closed
    over a fortnight misses two weekly cycles, and both must arrive in
    Review rather than be lost. Queueing is all this does — nothing is
    sent, and nothing posts, until a person approves it.

    Never allowed to stop the app starting. A scheduling problem is not
    a reason to be unable to open your books.
    """
    try:
        import recurring_invoices
        summary = recurring_invoices.run_due_checks()
        if summary["queued"]:
            print(f"Recurring invoices: queued {summary['queued']} for review.")
    except Exception as exc:  # noqa: BLE001 - startup must not be fragile
        print(f"Recurring invoice catch-up failed: {exc}")


def _wait_for_backend(supervisor=None, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _server_is_klearing():
            return True
        if supervisor is not None and supervisor.process.poll() is not None:
            # Exit 3 means the OS lock found an already-starting backend.
            # Give that owner the rest of the readiness window, then attach.
            if supervisor.process.returncode != 3:
                return False
        time.sleep(0.2)
    return False


def _serve_in_browser(supervisor):
    """Fallback when no native webview is available: the pre-native
    behavior — browser tab against the local server, process stays
    alive until killed."""
    webbrowser.open(APP_URL)
    if supervisor is not None:
        try:
            supervisor.process.wait()
        finally:
            supervisor.close()
    return 0


def _run_backend(supervision_fd=None):
    """Own the database/server role under an OS lock.

    A private child watches ``supervision_fd``. EOF means its shell is gone;
    it requests a graceful uvicorn stop. The Windows Job Object remains the
    hard-stop guarantee if graceful code cannot run.
    """
    _route_output_to("app")
    try:
        instance_lock = process_supervision.BackendInstanceLock().acquire()
    except process_supervision.BackendAlreadyRunning as exc:
        _log_lifecycle(f"backend start refused: {exc}")
        print(str(exc))
        return 3

    try:
        import uvicorn
        import db
        db.init_db()
        # Storage compatibility is established before app.py's import-time
        # initialization can run.
        from app import app

        _catch_up_recurring_invoices()
        config = uvicorn.Config(
            app, host=LOCAL_BIND_HOST, port=PORT,
            log_level="warning", access_log=False,
        )
        server = uvicorn.Server(config)

        if supervision_fd is not None:
            def watch_shell():
                try:
                    while os.read(supervision_fd, 1):
                        pass
                except OSError:
                    pass
                finally:
                    server.should_exit = True

            threading.Thread(
                target=watch_shell, daemon=True,
                name="klearing-shell-supervision",
            ).start()

        _log_lifecycle(f"backend started pid={os.getpid()}")
        server.run()
        return 0
    finally:
        if supervision_fd is not None:
            try:
                os.close(supervision_fd)
            except OSError:
                pass
        instance_lock.close()
        _log_lifecycle(f"backend stopped pid={os.getpid()}")


def serve_native():
    """Default and only packaged user role: supervised backend + window."""
    _route_output_to("app")

    supervisor = None
    if _server_is_klearing():
        _log_lifecycle(f"window attached to existing server on port {PORT}")
    else:
        _log_lifecycle("shell started")
        try:
            supervisor = process_supervision.spawn_backend()
        except process_supervision.BackendSupervisionError as exc:
            print(str(exc))
            return 1
        if not _wait_for_backend(supervisor):
            exit_code = supervisor.process.poll()
            supervisor.close()
            print(f"The supervised backend did not start on port {PORT} "
                  f"(exit {exit_code}); see app.log for details.")
            return 1

    try:
        import webview
    except Exception as exc:
        print(f"Native window unavailable ({exc}) — opening in the "
              f"browser instead.")
        return _serve_in_browser(supervisor)

    # Downloads are off by default in pywebview; the Export backup
    # button is a download, so it must be on.
    webview.settings["ALLOW_DOWNLOADS"] = True
    # (External target=_blank links already open in the OS browser:
    # OPEN_EXTERNAL_LINKS_IN_BROWSER defaults to True.)

    def shutdown():
        """Close the pipe, reap the child, then forcefully end GUI threads."""
        if supervisor is not None:
            supervisor.close()
        _log_lifecycle("shell stopped")
        sys.stdout.flush()
        os._exit(0)

    # The icon argument only takes effect on Linux GUIs; Windows and
    # macOS take the window icon from the exe / app bundle, which the
    # PyInstaller spec already sets.
    icon = paths.resource_path("assets/klearing.ico")
    try:
        window = webview.create_window(
            APP_NAME, APP_URL,
            width=1200, height=800, min_size=(940, 600),
        )
        # Fires on the GUI thread the instant the window closes —
        # the reliable shutdown signal across backends.
        window.events.closed += shutdown
        webview.start(icon=str(icon) if icon.exists() else None)
    except Exception as exc:
        print(f"Native window failed to start ({exc}) — opening in the "
              f"browser instead.")
        return _serve_in_browser(supervisor)

    # Backends where start() DOES return on close land here — shut down
    # the same way.
    shutdown()
    return 0


def serve_headless():
    """Development-only server role; packaged users cannot bypass the shell."""
    if paths.is_frozen():
        print("--serve is development-only. Start Klearing.exe without flags.")
        return 2
    return _run_backend()


def serve_backend_child():
    if not process_supervision.internal_child_authorized():
        print("The backend child role is internal; start Klearing normally.")
        return 2
    return _run_backend(process_supervision.child_supervision_fd())


def daily_check():
    _route_output_to("daily")
    print(f"--- {APP_NAME} daily check, "
          f"{datetime.now(timezone.utc).isoformat()} ---")
    import db
    # Refuse before importing/running any unattended integration work. Several
    # daily substeps deliberately isolate ordinary failures; compatibility is
    # different and must stop the whole run before a source can write.
    db.init_db()
    import check_bank
    return check_bank.run_daily()


def weekly_digest():
    _route_output_to("weekly")
    print(f"--- {APP_NAME} weekly digest, "
          f"{datetime.now(timezone.utc).isoformat()} ---")
    import db
    db.init_db()
    import weekly_digest as digest
    return digest.run_weekly()


def main(argv):
    if argv == ["--version"]:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    try:
        if argv == ["--daily-check"]:
            return daily_check()
        if argv == ["--weekly-digest"]:
            return weekly_digest()
        if argv == ["--serve"]:
            return serve_headless()
        if argv == [process_supervision.BACKEND_CHILD_FLAG]:
            return serve_backend_child()
        if not argv:
            return serve_native()
        print("Unknown command. Start Klearing without flags.")
        return 2
    except StorageCompatibilityError as exc:
        if argv == ["--daily-check"]:
            return _handle_storage_refusal(exc, "daily")
        if argv == ["--weekly-digest"]:
            return _handle_storage_refusal(exc, "weekly")
        return _handle_storage_refusal(
            exc, "app", native=not argv)


if __name__ == "__main__":
    multiprocessing.freeze_support()  # required in frozen Windows builds
    sys.exit(main(sys.argv[1:]))
