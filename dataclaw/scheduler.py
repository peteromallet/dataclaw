"""OS scheduler integration for DataClaw auto mode."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

_ENV_ALLOWLIST = ("PATH", "HOME", "HF_HOME", "HUGGINGFACE_HUB_CACHE", "LANG", "LC_ALL", "TMPDIR")
LAUNCH_LABEL = "io.dataclaw.auto"
LAUNCH_PLIST = Path.home() / "Library/LaunchAgents" / f"{LAUNCH_LABEL}.plist"
SYSTEMD_DIR = Path.home() / ".config/systemd/user"
SERVICE_NAME = "dataclaw-auto.service"
TIMER_NAME = "dataclaw-auto.timer"
LOG_OUT = Path.home() / ".dataclaw/logs/auto.out.log"
LOG_ERR = Path.home() / ".dataclaw/logs/auto.err.log"


def _capture_env(allowlist: tuple[str, ...] = _ENV_ALLOWLIST) -> dict[str, str]:
    return {key: os.environ[key] for key in allowlist if key in os.environ}


def _binary_path(config: dict) -> str:
    auto = config.get("auto")
    if isinstance(auto, dict) and auto.get("binary"):
        return str(auto["binary"])
    return "dataclaw"


def _parse_time(hhmm: str) -> tuple[int, int]:
    try:
        hour_s, minute_s = hhmm.split(":", 1)
        hour = int(hour_s)
        minute = int(minute_s)
    except (TypeError, ValueError):
        raise ValueError("time must be HH:MM")
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("time must be HH:MM")
    return hour, minute


def _chmod_600(path: Path) -> None:
    os.chmod(path, 0o600)


def install_macos(config: dict, time_hhmm: str = "03:00") -> Path:
    hour, minute = _parse_time(time_hhmm)
    LAUNCH_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG_OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [_binary_path(config), "auto"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(LOG_OUT),
        "StandardErrorPath": str(LOG_ERR),
        "EnvironmentVariables": _capture_env(),
    }
    LAUNCH_PLIST.write_bytes(plistlib.dumps(payload))
    _chmod_600(LAUNCH_PLIST)
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(LAUNCH_PLIST)],
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(LAUNCH_PLIST)],
        check=True,
    )
    return LAUNCH_PLIST


def _systemd_env_lines() -> list[str]:
    return [
        f"Environment={key}={shlex.quote(value)}"
        for key, value in _capture_env().items()
    ]


def install_linux(config: dict, time_hhmm: str = "03:00") -> Path:
    hour, minute = _parse_time(time_hhmm)
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    LOG_OUT.parent.mkdir(parents=True, exist_ok=True)
    service_path = SYSTEMD_DIR / SERVICE_NAME
    timer_path = SYSTEMD_DIR / TIMER_NAME
    env_lines = "\n".join(_systemd_env_lines())
    service_path.write_text(
        "\n".join([
            "[Unit]",
            "Description=DataClaw automatic export",
            "",
            "[Service]",
            "Type=oneshot",
            *([env_lines] if env_lines else []),
            f"ExecStart={_binary_path(config)} auto",
            f"StandardOutput=append:{LOG_OUT}",
            f"StandardError=append:{LOG_ERR}",
            "",
        ])
    )
    timer_path.write_text(
        "\n".join([
            "[Unit]",
            "Description=Run DataClaw automatic export daily",
            "",
            "[Timer]",
            f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00",
            "Persistent=true",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ])
    )
    _chmod_600(service_path)
    _chmod_600(timer_path)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", TIMER_NAME], check=True)
    return timer_path


def uninstall_macos() -> None:
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(LAUNCH_PLIST)],
        stderr=subprocess.DEVNULL,
        check=False,
    )
    try:
        LAUNCH_PLIST.unlink()
    except FileNotFoundError:
        pass


def uninstall_linux() -> None:
    subprocess.run(["systemctl", "--user", "disable", "--now", TIMER_NAME], check=False)
    for path in (SYSTEMD_DIR / SERVICE_NAME, SYSTEMD_DIR / TIMER_NAME):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)


def status() -> dict:
    if sys.platform == "darwin":
        if not LAUNCH_PLIST.exists():
            return {"installed": False, "platform": "darwin", "path": str(LAUNCH_PLIST)}
        proc = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_LABEL}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "installed": True,
            "enabled": proc.returncode == 0,
            "platform": "darwin",
            "path": str(LAUNCH_PLIST),
            "details": proc.stdout,
        }

    timer_path = SYSTEMD_DIR / TIMER_NAME
    proc = subprocess.run(
        ["systemctl", "--user", "is-enabled", TIMER_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "installed": timer_path.exists(),
        "enabled": proc.returncode == 0 and proc.stdout.strip() == "enabled",
        "platform": "linux",
        "path": str(timer_path),
        "details": proc.stdout.strip() or proc.stderr.strip(),
    }


def notify(title: str, body: str) -> None:
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
        subprocess.run(["osascript", "-e", script], check=False)
    else:
        subprocess.run(["notify-send", title, body], check=False)
