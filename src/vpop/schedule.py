"""
`vpop schedule`: keep the synced data fresh with a systemd user timer that runs `vpop sync`.

`install` writes `vpop-sync.service` (one `vpop -q sync`) and `vpop-sync.timer` (when to run
it) to the user's systemd directory and starts the timer; `remove` stops it and deletes
both; `status` shows the next run and how each source's last sync went. A sync the timer
runs has no terminal, so it never opens a browser: an expired Google token is recorded as
an error saying to run `vpop auth google`.
"""

import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from vpop.fsutil import atomic_write
from vpop.paths import systemd_user_dir
from vpop.sources.sync import read_status

SERVICE = "vpop-sync.service"
TIMER = "vpop-sync.timer"

# Runs `systemctl --user` with these arguments and returns its output (`systemctl`, unless a
# test stands in for it).
Runner = Callable[[Sequence[str]], str]


class ScheduleError(RuntimeError):
    """The timer can't be set up or read. The message says why."""


def systemctl(args: Sequence[str]) -> str:
    """Run `systemctl --user` with `args`, raising `ScheduleError` if it fails."""
    try:
        done = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise ScheduleError(
            "systemctl isn't available; vpop schedule needs systemd"
        ) from None
    if done.returncode:
        raise ScheduleError(
            f"systemctl --user {' '.join(args)} failed: {done.stderr.strip()}"
        )
    return done.stdout


def vpop_command() -> str:
    """The absolute path of the `vpop` on the PATH, for the service to run."""
    found = shutil.which("vpop")
    if found is None:
        raise ScheduleError(
            "vpop isn't on your PATH; install it first with `uv tool install .`"
        )
    return str(Path(found).absolute())


def units(vpop: str, every: str) -> dict[str, str]:
    """The service and timer files, by name: run `vpop` to sync, `every` (OnCalendar) time."""
    if not every.strip() or "\n" in every:
        raise ScheduleError(f"--every must be a systemd calendar time, not {every!r}")
    command = f'"{vpop}"' if re.search(r"\s", vpop) else vpop
    return {
        SERVICE: (
            "[Unit]\n"
            "Description=Sync vPop's data sources\n"
            "\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"ExecStart={command} -q sync\n"
        ),
        TIMER: (
            "[Unit]\n"
            "Description=Sync vPop's data sources on a schedule\n"
            "\n"
            "[Timer]\n"
            f"OnCalendar={every.strip()}\n"
            # Catch up on a run missed while the machine was off.
            "Persistent=true\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        ),
    }


def install(every: str = "hourly", run: Runner | None = None) -> Path:
    """Write the units, start the timer, and return the directory they're in."""
    run = run or systemctl
    directory = systemd_user_dir()
    for name, text in units(vpop_command(), every).items():
        atomic_write(directory / name, text)
    run(["daemon-reload"])
    run(["enable", "--now", TIMER])
    return directory


def remove(run: Runner | None = None) -> bool:
    """Stop the timer and delete the units; False if they weren't installed."""
    run = run or systemctl
    directory = systemd_user_dir()
    if not (directory / TIMER).exists():
        return False
    run(["disable", "--now", TIMER])
    for name in (TIMER, SERVICE):
        (directory / name).unlink(missing_ok=True)
    run(["daemon-reload"])
    return True


def status(run: Runner | None = None) -> str:
    """The timer's state and next run, then each source's last sync."""
    run = run or systemctl
    timer = systemd_user_dir() / TIMER
    if not timer.exists():
        lines = ["timer: not installed; run `vpop schedule install`"]
    else:
        every = re.search(r"^OnCalendar=(.*)$", timer.read_text(), re.MULTILINE)
        state = run(["show", TIMER, "--property=ActiveState", "--value"]).strip()
        upcoming = run(
            ["show", TIMER, "--property=NextElapseUSecRealtime", "--value"]
        ).strip()
        lines = [
            f"timer: {state}, {every[1] if every else 'unknown schedule'}",
            f"next run: {upcoming or 'none scheduled'}",
        ]
    statuses = read_status()
    lines += [f"{name}: {statuses[name].describe()}" for name in sorted(statuses)]
    return "\n".join(lines)
