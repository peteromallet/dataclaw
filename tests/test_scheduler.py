"""Tests for dataclaw.scheduler OS integration helpers."""

import os
import plistlib
from types import SimpleNamespace

import dataclaw.scheduler as scheduler


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_capture_env_filters_to_allowlist_excludes_hf_token(monkeypatch):
    monkeypatch.setattr(os, "environ", {
        "PATH": "/bin",
        "HOME": "/tmp/home",
        "HF_HOME": "/tmp/hf",
        "HF_TOKEN": "secret",
        "UNRELATED": "ignored",
    })

    captured = scheduler._capture_env()

    assert captured == {"PATH": "/bin", "HOME": "/tmp/home", "HF_HOME": "/tmp/hf"}
    assert "HF_TOKEN" not in captured


def test_install_schedule_macos_plist_contents_and_chmod_600(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "LAUNCH_PLIST", tmp_path / "io.dataclaw.auto.plist")
    monkeypatch.setattr(scheduler, "LOG_OUT", tmp_path / "logs" / "auto.out.log")
    monkeypatch.setattr(scheduler, "LOG_ERR", tmp_path / "logs" / "auto.err.log")
    monkeypatch.setattr(os, "environ", {"PATH": "/bin", "HF_TOKEN": "secret"})
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)) or _proc())

    path = scheduler.install_macos({"auto": {"binary": "/usr/local/bin/dataclaw"}}, "03:00")

    plist = plistlib.loads(path.read_bytes())
    assert plist["Label"] == "io.dataclaw.auto"
    assert plist["ProgramArguments"][1] == "auto"
    assert "HF_TOKEN" not in plist["EnvironmentVariables"]
    assert "HF_TOKEN" not in path.read_text(errors="ignore")
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert calls[0][0][0][1] == "bootout"
    assert calls[1][0][0][1] == "bootstrap"


def test_install_schedule_linux_systemd_unit_contents_and_chmod_600(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "SYSTEMD_DIR", tmp_path / "systemd")
    monkeypatch.setattr(scheduler, "LOG_OUT", tmp_path / "logs" / "auto.out.log")
    monkeypatch.setattr(scheduler, "LOG_ERR", tmp_path / "logs" / "auto.err.log")
    monkeypatch.setattr(os, "environ", {"PATH": "/bin", "HF_TOKEN": "secret"})
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)) or _proc())

    timer_path = scheduler.install_linux({"auto": {"binary": "dataclaw"}}, "03:00")

    service_path = tmp_path / "systemd" / scheduler.SERVICE_NAME
    service = service_path.read_text()
    timer = timer_path.read_text()
    assert "dataclaw auto" in service
    assert "OnCalendar=*-*-* 03:00:00" in timer
    assert "HF_TOKEN=" not in service
    assert "HF_TOKEN" not in service
    assert "HF_TOKEN" not in timer
    assert oct(service_path.stat().st_mode)[-3:] == "600"
    assert oct(timer_path.stat().st_mode)[-3:] == "600"
    assert calls[-1][0][0] == ["systemctl", "--user", "enable", "--now", scheduler.TIMER_NAME]


def test_uninstall_macos_removes_plist(tmp_path, monkeypatch):
    plist = tmp_path / "io.dataclaw.auto.plist"
    plist.write_text("plist")
    calls = []
    monkeypatch.setattr(scheduler, "LAUNCH_PLIST", plist)
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)) or _proc())

    scheduler.uninstall_macos()

    assert not plist.exists()
    assert calls[0][0][0][1] == "bootout"


def test_uninstall_linux_removes_units_and_daemon_reload(tmp_path, monkeypatch):
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    service = systemd_dir / scheduler.SERVICE_NAME
    timer = systemd_dir / scheduler.TIMER_NAME
    service.write_text("service")
    timer.write_text("timer")
    calls = []
    monkeypatch.setattr(scheduler, "SYSTEMD_DIR", systemd_dir)
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)) or _proc())

    scheduler.uninstall_linux()

    assert not service.exists()
    assert not timer.exists()
    assert calls[-1][0][0] == ["systemctl", "--user", "daemon-reload"]


def test_status_macos_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler.sys, "platform", "darwin")
    monkeypatch.setattr(scheduler, "LAUNCH_PLIST", tmp_path / "missing.plist")

    status = scheduler.status()

    assert status["installed"] is False
    assert status["platform"] == "darwin"


def test_status_linux_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler.sys, "platform", "linux")
    monkeypatch.setattr(scheduler, "SYSTEMD_DIR", tmp_path / "systemd")
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *args, **kwargs: _proc(returncode=1, stdout="disabled\n"))

    status = scheduler.status()

    assert status["installed"] is False
    assert status["enabled"] is False
    assert status["details"] == "disabled"
