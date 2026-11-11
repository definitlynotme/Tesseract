"""Windows 'launch at startup' toggle via HKCU Run registry key.

Clash Verge exposes an "Auto Launch" switch that registers the app with
Windows Startup. This module provides the equivalent primitive so the
desktop layer can opt the user in without shipping a second installer.

Design notes:
- HKCU scope only. HKLM would require elevation and affect all users.
- We write the raw path+arguments with double-quotes around the executable
  to tolerate paths with spaces (very common under ``Program Files``).
- Non-Windows platforms and missing ``winreg`` module become no-ops and
  return an explicit disabled/unsupported state instead of raising.
- When the app is run from the Python source tree (``python
  desktop_app.pyw``) we register the interpreter + script so developers
  get the same behaviour as installed users.
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any


REG_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_REG_VALUE_NAME = "Tesseract"


def _is_windows() -> bool:
    return platform.system().lower() == "windows"


def _import_winreg():
    if not _is_windows():
        return None
    try:
        import winreg  # noqa: WPS433 - Windows-only stdlib module

        return winreg
    except Exception:
        return None


def _quote(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    if value.startswith('"') and value.endswith('"'):
        return value
    return f'"{value}"'


def _build_launch_command(
    executable: str | None = None,
    script: str | None = None,
    extra_args: list[str] | None = None,
    launch_target: str = "legacy_qt",
) -> str:
    """Compose the command that Windows should run at login.

    Prefer a frozen bundle entry point; fall back to Python interpreter +
    ``desktop_app.pyw`` when running from source.
    """
    args: list[str] = []
    exe = executable or sys.executable or ""
    frozen_exe = getattr(sys, "frozen", False)
    if (frozen_exe or launch_target == "shell") and exe:
        args.append(_quote(exe))
    else:
        args.append(_quote(exe or sys.executable))
        if script:
            args.append(_quote(script))
        else:
            candidate = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
            if candidate and candidate.exists():
                args.append(_quote(str(candidate)))
    if extra_args:
        args.extend(str(arg) for arg in extra_args if str(arg))
    return " ".join(part for part in args if part)


class AutostartManager:
    """Tiny wrapper around HKCU ``Run`` registration."""

    def __init__(
        self,
        *,
        value_name: str = DEFAULT_REG_VALUE_NAME,
        executable: str | None = None,
        script: str | None = None,
        extra_args: list[str] | None = None,
        launch_target: str = "legacy_qt",
    ) -> None:
        if launch_target not in {"legacy_qt", "shell"}:
            raise ValueError("unsupported autostart launch target")
        self.value_name = value_name
        self.executable = executable
        self.script = script
        self.extra_args = list(extra_args or [])
        self.launch_target = launch_target

    # ------------------------------------------------------------------ state

    @property
    def supported(self) -> bool:
        return _import_winreg() is not None

    def launch_command(self) -> str:
        return _build_launch_command(
            self.executable, self.script, self.extra_args, self.launch_target,
        )

    def is_enabled(self) -> bool:
        winreg = _import_winreg()
        if winreg is None:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, self.value_name)
                return bool(value)
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def current_command(self) -> str | None:
        winreg = _import_winreg()
        if winreg is None:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, self.value_name)
                return str(value) if value else None
        except FileNotFoundError:
            return None
        except OSError:
            return None

    # -------------------------------------------------------------- mutations

    def enable(self) -> dict[str, Any]:
        winreg = _import_winreg()
        if winreg is None:
            return {"supported": False, "enabled": False, "command": None, "error": "autostart is Windows-only"}
        command = self.launch_command()
        if not command:
            return {"supported": True, "enabled": False, "command": None, "error": "failed to compose launch command"}
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, command)
            return {"supported": True, "enabled": True, "command": command, "error": None}
        except OSError as exc:
            return {"supported": True, "enabled": False, "command": command, "error": str(exc)}

    def disable(self) -> dict[str, Any]:
        winreg = _import_winreg()
        if winreg is None:
            return {"supported": False, "enabled": False, "command": None, "error": "autostart is Windows-only"}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
                try:
                    winreg.DeleteValue(key, self.value_name)
                except FileNotFoundError:
                    pass
            return {"supported": True, "enabled": False, "command": None, "error": None}
        except OSError as exc:
            return {"supported": True, "enabled": False, "command": None, "error": str(exc)}

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        return self.enable() if enabled else self.disable()

    def status(self) -> dict[str, Any]:
        supported = self.supported
        return {
            "supported": supported,
            "enabled": self.is_enabled() if supported else False,
            "command": self.launch_command(),
            "current_command": self.current_command() if supported else None,
            "value_name": self.value_name,
            "frozen": bool(getattr(sys, "frozen", False)),
        }


def _current_script_hint() -> str | None:
    candidate = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if candidate and candidate.exists():
        return str(candidate)
    return None


def default_autostart_manager() -> AutostartManager:
    """Build the default manager for the running interpreter/bundle."""
    if getattr(sys, "frozen", False):
        return AutostartManager(executable=sys.executable)
    return AutostartManager(executable=sys.executable, script=_current_script_hint())


def shell_autostart_manager(executable: str) -> AutostartManager:
    """Build a shell command from a bootstrap-validated executable path.

    The sidecar must use bridge_autostart.sidecar_autostart_manager() to validate
    the installation layout before calling this primitive; never use IPC paths.
    """
    if not executable:
        raise ValueError("shell executable is required")
    return AutostartManager(
        executable=executable, launch_target="shell", extra_args=["--autostart"],
    )
