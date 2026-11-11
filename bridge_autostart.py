"""Trusted sidecar bootstrap and read-only, path-free autostart projection.

No function in this module is an IPC handler accepting a page-supplied path.
The optional shell path belongs exclusively to trusted bootstrap code.
"""
from __future__ import annotations

import ntpath
import re
import sys
from pathlib import Path
from typing import Any

import autostart


SHELL_FILENAMES = ("Tesseract.exe", "tesseract-shell.exe")
_SHELL_NAMES = {name.casefold() for name in SHELL_FILENAMES}


def validate_shell_path(sidecar: Path, shell: Path) -> Path:
    """Require an existing whitelist executable beside the frozen sidecar.

    Resolve links before comparing directories; neither links outside the
    installation nor a hard link to the sidecar can become a launch target.
    """
    if not sidecar.is_absolute() or not shell.is_absolute():
        raise ValueError("shell_path_invalid")
    if shell.name.casefold() not in _SHELL_NAMES:
        raise ValueError("shell_path_invalid")
    try:
        resolved_sidecar = sidecar.resolve(strict=True)
        resolved_shell = shell.resolve(strict=True)
        if not resolved_sidecar.is_file() or not resolved_shell.is_file():
            raise ValueError("shell_path_invalid")
        if (resolved_shell.name.casefold() not in _SHELL_NAMES
                or resolved_shell.parent != resolved_sidecar.parent
                or resolved_shell.samefile(resolved_sidecar)):
            raise ValueError("shell_path_invalid")
    except FileNotFoundError:
        raise ValueError("shell_missing") from None
    except (OSError, RuntimeError):
        raise ValueError("shell_path_invalid") from None
    return resolved_shell


def _error(reason: str) -> dict[str, Any]:
    code = "validation" if reason == "development_mode" else "io"
    return {"code": code, "message_key": f"errors.autostart.{reason}", "params": {}}


def _command_parts(command: str) -> tuple[str, str] | None:
    # Run is a direct executable command, not a cmd.exe command. Reject
    # ambiguous unquoted paths with spaces instead of guessing an executable.
    match = re.fullmatch(r'\s*"([^"]+)"(?:\s+(.*))?\s*', command)
    if match is None:
        match = re.fullmatch(r'\s*([^\s"]+)(?:\s+(.*))?\s*', command)
    return (match[1], (match[2] or "").strip()) if match else None


def _path_identity(path: str) -> str | None:
    try:
        candidate = Path(path)
        if not candidate.is_absolute():
            return None
        return ntpath.normcase(str(candidate.resolve()))
    except (OSError, ValueError, RuntimeError):
        return None


class SidecarAutostartManager:
    """AutostartManager-compatible facade with validated shell-only writes."""

    def __init__(self, sidecar: Path, shell: Path | None, reason: str | None) -> None:
        self._sidecar, self._shell, self._reason = sidecar, shell, reason
        self._manager = autostart.shell_autostart_manager(str(shell)) if shell else None
        self.value_name = autostart.DEFAULT_REG_VALUE_NAME

    def _layout_error(self) -> str | None:
        if self._reason:
            return self._reason
        try:
            validate_shell_path(self._sidecar, self._shell)
        except ValueError as exc:
            return str(exc)
        return None

    @property
    def supported(self) -> bool:
        return self._layout_error() is None and autostart._import_winreg() is not None

    def launch_command(self) -> str:
        return self._manager.launch_command() if self._layout_error() is None else ""

    def current_command(self) -> str | None:
        # Raw values are for trusted runtime code only. Pages use state_dto().
        return autostart.AutostartManager(value_name=self.value_name).current_command()

    def is_enabled(self) -> bool:
        return bool(self.current_command())

    def enable(self) -> dict[str, Any]:
        reason = self._layout_error()
        if reason:
            return {"supported": False, "enabled": self.is_enabled(),
                    "command": None, "error": reason}
        self._manager.value_name = self.value_name
        return self._manager.enable()

    def disable(self) -> dict[str, Any]:
        # Explicit settings actions may remove a stale registration even when
        # the shell has disappeared. Queries never reach this mutation.
        return autostart.AutostartManager(value_name=self.value_name).disable()

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        return self.enable() if enabled else self.disable()

    def status(self) -> dict[str, Any]:
        return autostart_state(self)


class IsolatedAutostartManager(SidecarAutostartManager):
    """Read-only facade for scopes that may not touch the host (T-P2-31b fix round 1).

    The HKCU Run value is shared with the installed app (Qt or shell), so a
    custom/audit scope (``may_take_over_host_network=False``) reports it but
    never enables or removes it: both writes answer ``development_mode``.
    """

    def _layout_error(self) -> str | None:
        return super()._layout_error() or "development_mode"

    def disable(self) -> dict[str, Any]:
        return {"supported": False, "enabled": self.is_enabled(),
                "command": None, "error": self._layout_error()}


def isolated_autostart_manager(manager: Any) -> Any:
    """The read-only variant of a sidecar manager; anything else unchanged."""
    if isinstance(manager, SidecarAutostartManager) and not isinstance(manager, IsolatedAutostartManager):
        isolated = IsolatedAutostartManager.__new__(IsolatedAutostartManager)
        isolated.__dict__.update(manager.__dict__)
        return isolated
    return manager


def sidecar_autostart_manager(
    *, shell_executable: str | Path | None = None,
) -> SidecarAutostartManager:
    """Build before runtime construction; never trust environment/IPC paths.

    Release layout prefers Tesseract.exe; the development frozen shell uses
    tesseract-shell.exe. Source mode never registers the Python interpreter.
    """
    sidecar = Path(sys.executable)
    if not getattr(sys, "frozen", False):
        return SidecarAutostartManager(sidecar, None, "development_mode")
    try:
        if shell_executable is None:
            shell = sidecar.with_name(SHELL_FILENAMES[0])
            if not shell.exists():
                shell = sidecar.with_name(SHELL_FILENAMES[1])
        else:
            shell = Path(shell_executable)
        validated = validate_shell_path(sidecar, shell)
    except ValueError as exc:
        reason = str(exc) if str(exc) in {"shell_missing", "shell_path_invalid"} else "shell_path_invalid"
        return SidecarAutostartManager(sidecar, None, reason)
    except OSError:
        return SidecarAutostartManager(sidecar, None, "shell_path_invalid")
    return SidecarAutostartManager(sidecar, validated, None)


def autostart_state(manager: SidecarAutostartManager) -> dict[str, Any]:
    """Read HKCU once, report drift, and never repair or reveal paths."""
    reason = manager._layout_error()
    registry = autostart._import_winreg()
    state: dict[str, Any] = {
        "supported": reason is None and registry is not None,
        "enabled": False, "target": "none", "mismatch": False,
        "error": _error(reason) if reason else None,
    }
    if registry is None:
        state["error"] = state["error"] or _error("registry_unavailable")
        return state
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, autostart.REG_KEY_PATH,
                              0, registry.KEY_READ) as key:
            command, kind = registry.QueryValueEx(key, manager.value_name)
    except FileNotFoundError:
        return state
    except OSError:
        state["error"] = _error("read_failed")
        return state
    if not command:
        return state
    state.update(enabled=True, target="other", mismatch=True)
    if not isinstance(command, str) or kind != registry.REG_SZ:
        state["error"] = _error("registration_invalid")
        return state
    parts = _command_parts(command)
    if parts is None:
        return state
    executable, arguments = parts
    identity = _path_identity(executable)
    if reason is None and identity == _path_identity(str(manager._shell)):
        state["target"] = "shell"
        state["mismatch"] = arguments != "--autostart"
    elif identity is not None and ntpath.basename(executable).casefold() == "tesseract.exe":
        state["target"] = "legacy_qt"
    return state
