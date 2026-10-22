"""
Scheduled-task management for Klearing — all driven from the GUI
(Settings → Automation), no terminal required.

Three jobs:
- app     start Klearing at login (so the dashboard is always there)
- daily   the 8:00 AM check (bank fetch, alerts, drafts, GA4 snapshot)
- weekly  the Monday 8:00 AM digest email

Windows: Task Scheduler, per-user, no admin. schtasks has no flag for
either a user-scoped logon trigger or StartWhenAvailable (catch up
after the machine was asleep at 8), so each job is registered by
importing a task XML — which works unelevated. Tasks run as the
logged-on user (InteractiveToken), which is exactly what lets the
background run read the master key from Credential Manager without
any prompt.

macOS: launchd LaunchAgents in ~/Library/LaunchAgents, loaded with
`launchctl bootstrap gui/<uid>`. StartCalendarInterval fires missed
schedules on wake (unlike cron); runs missed while powered off are
skipped, and the login-time app start is the catch-up. macOS 13+
shows a "background items added" notification the first time — that's
expected.
"""

import subprocess
import sys
from datetime import date, timedelta
from xml.sax.saxutils import escape as xml_escape

import paths
from config import APP_NAME

JOBS = {
    "app": {
        "title": f"{APP_NAME} App",
        "description": f"Opens {APP_NAME} in its window at login.",
        # No args = the native-window role.
        "args": [],
    },
    "daily": {
        "title": f"{APP_NAME} Daily Check",
        "description": "Fetches bank transactions and runs the daily "
                       "checks at 8:00 AM.",
        "args": ["--daily-check"],
    },
    "weekly": {
        "title": f"{APP_NAME} Weekly Digest",
        "description": "Emails the weekly digest on Monday mornings.",
        "args": ["--weekly-digest"],
    },
}


def is_supported():
    return sys.platform in ("win32", "darwin")


def _run(command, **kwargs):
    """Run a command with no console window flash on Windows. Output is
    decoded permissively — schtasks/whoami emit OEM-codepage bytes that
    can trip strict decoding on non-English systems, and a mangled
    accent in a message must not crash task management."""
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", 0x08000000)  # CREATE_NO_WINDOW
    return subprocess.run(command, capture_output=True, text=True,
                          errors="replace", **kwargs)


# ---------------------------------------------------------------- Windows


def _task_name(job):
    return JOBS[job]["title"]


def _current_user_sid():
    result = _run(["whoami", "/user", "/fo", "csv"])
    # Last CSV field of the last line is the SID.
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    if len(lines) >= 2:
        sid = lines[-1].rsplit(",", 1)[-1].strip().strip('"')
        if sid.startswith("S-1-"):
            return sid
    raise RuntimeError("Could not determine the current user's SID.")


def _windows_task_xml(job):
    command, *arguments = paths.app_command(*JOBS[job]["args"])
    args_text = xml_escape(" ".join(
        f'"{a}"' if " " in a else a for a in arguments
    ))
    command = xml_escape(command)
    sid = _current_user_sid()

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    if job == "daily":
        trigger = f"""
    <CalendarTrigger>
      <StartBoundary>{tomorrow}T08:00:00</StartBoundary>
      <EndBoundary>2099-12-31T00:00:00</EndBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>"""
    elif job == "weekly":
        trigger = f"""
    <CalendarTrigger>
      <StartBoundary>{tomorrow}T08:00:00</StartBoundary>
      <EndBoundary>2099-12-31T00:00:00</EndBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek><Monday /></DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>"""
    else:  # app: start at logon (user-scoped — this is what avoids admin)
        trigger = f"""
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{sid}</UserId>
    </LogonTrigger>"""

    # The app job is a long-running server — a time limit would kill it
    # mid-day. PT0S disables the limit; the batch jobs keep a 2h cap so
    # a hung check can't pile up forever.
    time_limit = "PT0S" if job == "app" else "PT2H"

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{JOBS[job]["description"]}</Description>
  </RegistrationInfo>
  <Triggers>{trigger}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>{time_limit}</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{args_text}</Arguments>
    </Exec>
  </Actions>
</Task>"""


def _install_windows(job):
    xml = _windows_task_xml(job)
    xml_path = paths.data_dir() / f"task-{job}.xml"
    xml_path.write_text(xml, encoding="utf-16")
    result = _run(["schtasks", "/Create", "/TN", _task_name(job),
                   "/XML", str(xml_path), "/F"])
    if result.returncode != 0:
        raise RuntimeError(
            f"Task Scheduler refused the {job} task: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _uninstall_windows(job):
    _run(["schtasks", "/Delete", "/TN", _task_name(job), "/F"])


def _installed_windows(job):
    result = _run(["schtasks", "/Query", "/TN", _task_name(job)])
    return result.returncode == 0


# ---------------------------------------------------------------- macOS


def _agent_label(job):
    return f"com.klearing.{job}"


def _agent_path(job):
    from pathlib import Path
    return Path.home() / "Library" / "LaunchAgents" / f"{_agent_label(job)}.plist"


def _macos_plist(job):
    command = paths.app_command(*JOBS[job]["args"])
    program_args = "".join(
        f"\n      <string>{xml_escape(c)}</string>" for c in command
    )

    if job == "daily":
        schedule = """
    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key><integer>8</integer>
      <key>Minute</key><integer>0</integer>
    </dict>"""
    elif job == "weekly":
        schedule = """
    <key>StartCalendarInterval</key>
    <dict>
      <key>Weekday</key><integer>1</integer>
      <key>Hour</key><integer>8</integer>
      <key>Minute</key><integer>0</integer>
    </dict>"""
    else:
        schedule = """
    <key>RunAtLoad</key>
    <true/>"""

    log = paths.log_path("daily" if job == "daily"
                         else "weekly" if job == "weekly" else "app")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{_agent_label(job)}</string>
    <key>ProgramArguments</key>
    <array>{program_args}
    </array>{schedule}
    <key>StandardOutPath</key><string>{log}</string>
    <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>"""


def _uid():
    import os
    return os.getuid()


def _install_macos(job):
    path = _agent_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_macos_plist(job), encoding="utf-8")
    _run(["launchctl", "bootout", f"gui/{_uid()}/{_agent_label(job)}"])
    result = _run(["launchctl", "bootstrap", f"gui/{_uid()}", str(path)])
    if result.returncode != 0:
        raise RuntimeError(
            f"launchd refused the {job} agent: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _uninstall_macos(job):
    _run(["launchctl", "bootout", f"gui/{_uid()}/{_agent_label(job)}"])
    path = _agent_path(job)
    if path.exists():
        path.unlink()


def _installed_macos(job):
    result = _run(["launchctl", "print", f"gui/{_uid()}/{_agent_label(job)}"])
    return result.returncode == 0


# ---------------------------------------------------------------- public API


def install_all():
    """Register all three jobs. Raises RuntimeError with a readable
    message on the first failure."""
    if not is_supported():
        raise RuntimeError("Automatic scheduling is only wired up for "
                           "Windows and macOS on this build.")
    for job in JOBS:
        if sys.platform == "win32":
            _install_windows(job)
        else:
            _install_macos(job)


def uninstall_all():
    if not is_supported():
        return
    # "app" goes last: on macOS, booting out the agent that launched
    # the currently running server kills this very process — the other
    # two must already be gone by then.
    for job in ("daily", "weekly", "app"):
        if sys.platform == "win32":
            _uninstall_windows(job)
        else:
            _uninstall_macos(job)


def status():
    """{job: {'title': ..., 'installed': bool}} for the Automation page."""
    result = {}
    for job, spec in JOBS.items():
        if sys.platform == "win32":
            installed = _installed_windows(job)
        elif sys.platform == "darwin":
            installed = _installed_macos(job)
        else:
            installed = False
        result[job] = {"title": spec["title"],
                       "description": spec["description"],
                       "installed": installed}
    return result


def next_run_time(job):
    """The OS scheduler's next trigger time for one job, as the display
    string schtasks reports, or None when the job isn't registered, the
    query fails, or the platform can't answer (launchd has no next-fire
    query; the "app" job is logon-triggered so "next run" is N/A).
    Read-only and never raises — the Automation page renders a calm
    fallback for None."""
    try:
        if sys.platform != "win32":
            return None
        result = _run(["schtasks", "/Query", "/TN", _task_name(job),
                       "/FO", "LIST", "/V"])
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.lower().startswith("next run time:"):
                value = line.split(":", 1)[1].strip()
                if value and value.upper() != "N/A":
                    return value
                return None
    except Exception:
        return None
    return None


def run_job_now(job):
    """Launch one job detached, exactly as the scheduler would (its
    output lands in the same log the Logs page shows)."""
    command = paths.app_command(*JOBS[job]["args"])
    log_name = {"daily": "daily", "weekly": "weekly", "app": "app"}[job]
    log_file = open(paths.log_path(log_name), "a", encoding="utf-8")
    kwargs = {"stdout": log_file, "stderr": log_file}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    subprocess.Popen(command, **kwargs)
