"""Tests for `vpop schedule`, with systemctl stubbed out."""

import subprocess
from collections.abc import Sequence

import pytest

from vpop import cli, schedule
from vpop.paths import systemd_user_dir
from vpop.sources.sync import SyncStatus, write_status


class FakeSystemctl:
    """Records `systemctl --user` calls and answers `show` with canned values."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        if args[0] == "show":
            return {
                "--property=ActiveState": "active\n",
                "--property=NextElapseUSecRealtime": "Sun 2026-10-18 15:00:00 EDT\n",
            }[args[2]]
        return ""


@pytest.fixture
def systemctl(monkeypatch: pytest.MonkeyPatch) -> FakeSystemctl:
    fake = FakeSystemctl()
    monkeypatch.setattr(schedule, "systemctl", fake)
    monkeypatch.setattr(schedule.shutil, "which", lambda _: "/home/me/.local/bin/vpop")
    return fake


def test_units_run_a_quiet_sync_on_a_persistent_timer() -> None:
    units = schedule.units("/home/me/.local/bin/vpop", "hourly")
    assert units["vpop-sync.service"] == (
        "[Unit]\n"
        "Description=Sync vPop's data sources\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/home/me/.local/bin/vpop -q sync\n"
    )
    timer = units["vpop-sync.timer"]
    assert "[Timer]\nOnCalendar=hourly\nPersistent=true\n" in timer
    assert timer.endswith("[Install]\nWantedBy=timers.target\n")
    spaced = schedule.units("/home/me/my tools/vpop", "*:0/30")
    assert 'ExecStart="/home/me/my tools/vpop" -q sync\n' in spaced["vpop-sync.service"]
    assert "OnCalendar=*:0/30\n" in spaced["vpop-sync.timer"]


@pytest.mark.parametrize("every", ["", "daily\nExecStart=/bin/sh"])
def test_a_bad_schedule_is_refused(every: str) -> None:
    with pytest.raises(schedule.ScheduleError, match="--every"):
        schedule.units("/bin/vpop", every)


def test_install_status_and_remove(
    systemctl: FakeSystemctl, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["schedule", "install", "--every", "daily"])
    assert "vpop sync now runs daily" in capsys.readouterr().out
    assert "OnCalendar=daily" in (systemd_user_dir() / "vpop-sync.timer").read_text()
    assert systemctl.calls == [
        ["daemon-reload"],
        ["enable", "--now", "vpop-sync.timer"],
    ]

    write_status(
        {"whatsapp": SyncStatus("2026-10-18 14:00:00", "2026-10-18 14:00:00", None)}
    )
    cli.main(["schedule", "status"])
    assert capsys.readouterr().out.splitlines() == [
        "timer: active, daily",
        "next run: Sun 2026-10-18 15:00:00 EDT",
        "whatsapp: last synced 2026-10-18 14:00:00",
    ]

    systemctl.calls.clear()
    cli.main(["schedule", "remove"])
    assert systemctl.calls == [
        ["disable", "--now", "vpop-sync.timer"],
        ["daemon-reload"],
    ]
    assert not (systemd_user_dir() / "vpop-sync.service").exists()
    cli.main(["schedule", "remove"])
    assert capsys.readouterr().out.splitlines() == [
        "removed the sync timer",
        "no sync timer to remove",
    ]


def test_status_without_a_timer(
    systemctl: FakeSystemctl, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["schedule", "status"])
    assert capsys.readouterr().out == (
        "timer: not installed; run `vpop schedule install`\n"
    )
    assert systemctl.calls == []


def test_install_needs_vpop_on_the_path(
    systemctl: FakeSystemctl, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule.shutil, "which", lambda _: None)
    with pytest.raises(SystemExit, match="uv tool install"):
        cli.main(["schedule", "install"])
    assert systemctl.calls == []


def test_a_failing_systemctl_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 1, "", "Unit vpop-sync.timer not found.\n"
        )

    monkeypatch.setattr(schedule.subprocess, "run", fail)
    with pytest.raises(
        schedule.ScheduleError,
        match=r"systemctl --user enable --now vpop-sync\.timer failed: Unit vpop-sync",
    ):
        schedule.systemctl(["enable", "--now", "vpop-sync.timer"])

    def missing(*_: object, **__: object) -> None:
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(schedule.subprocess, "run", missing)
    with pytest.raises(schedule.ScheduleError, match="needs systemd"):
        schedule.systemctl(["daemon-reload"])
