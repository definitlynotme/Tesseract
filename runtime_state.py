"""Durable proxy journal under an asserted single-writer invariant.

Per 决定_P2-29_journal单写者.md and P3-02: the instance mutex excludes
other processes; writes here require this thread's proxy transaction or the
exclusive pre-runtime RT-1 worker phase. Runtime construction seals RT-1.
Snapshot comparisons are defense in depth, not an atomic concurrent CAS.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config_utils import coerce_bool
from models import utc_now_iso
from system_proxy import ProxySettings, assert_proxy_journal_writer


class ProxyJournalChanged(RuntimeError):
    """The recovery snapshot no longer belongs to the current transaction."""


@dataclass(frozen=True)
class StaleSystemProxyRecoveryPlan:
    should_restore: bool
    should_clear: bool
    original: ProxySettings | None


def stale_system_proxy_recovery_plan(
    state: dict[str, Any],
    *,
    current_target: str,
    target_active: bool,
) -> StaleSystemProxyRecoveryPlan:
    proxy_state = state.get("system_proxy") if isinstance(state, dict) else None
    if not isinstance(proxy_state, dict):
        return StaleSystemProxyRecoveryPlan(should_restore=False, should_clear=False, original=None)
    target = str(proxy_state.get("target") or "")
    if target != current_target:
        return StaleSystemProxyRecoveryPlan(should_restore=False, should_clear=False, original=None)
    if not target_active:
        return StaleSystemProxyRecoveryPlan(should_restore=False, should_clear=True, original=None)
    original_payload = proxy_state.get("original") or {}
    original = ProxySettings(
        enabled=coerce_bool(original_payload.get("enabled", False), False),
        server=str(original_payload.get("server", "")),
        override=str(original_payload.get("override", "")),
    )
    return StaleSystemProxyRecoveryPlan(should_restore=True, should_clear=True, original=original)


class RuntimeStateStore:
    def __init__(self, path: str | Path = "data/runtime-state.json") -> None:
        self.path = Path(path)
        self.proxy_transactions_managed = False
        self._legacy_clear_expected = None

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def load_proxy_journal(self) -> dict[str, Any]:
        """Strict transaction read: a corrupt/unreadable journal is not absent."""
        try:
            payload = json.loads(self.path.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(payload, dict):
            raise ValueError("invalid proxy journal")
        return payload

    def save_proxy_snapshot(
        self, *, original: ProxySettings, target: str,
        owner: str | None = None, phase: str = "prepared",
        expected: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert_proxy_journal_writer()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous = self.load_proxy_journal()
        if expected is not None and previous.get("system_proxy") != expected:
            raise ProxyJournalChanged("journal_changed")
        payload = {
            **previous,
            "generated_at": utc_now_iso(),
            "system_proxy": {
                "target": target,
                "original": asdict(original),
                "active": True,
                "owner": owner,
                "phase": phase,
            },
        }
        fd, temp_path = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if expected is not None and self.load_proxy_journal() != previous:
                raise ProxyJournalChanged("journal_changed")
            os.replace(temp_path, self.path)
            self._legacy_clear_expected = payload["system_proxy"] if owner is None else None
            # Return what THIS transaction committed, never adopt a reread.
            return payload["system_proxy"]
        except Exception:
            try:
                Path(temp_path).unlink()
            except FileNotFoundError:
                pass
            raise

    def clear_proxy_snapshot(self, expected: dict[str, Any]) -> bool:
        assert_proxy_journal_writer()
        # Reuse RT-1's defensive comparison; exclusivity comes from the phase/lock.
        from runtime_network_recovery import _clear_proxy_entry

        raw = self.path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("system_proxy") != expected:
            return False
        return _clear_proxy_entry(self.path, raw, payload, lambda: False, lambda: True)

    def clear(self) -> None:
        # Legacy DesktopRuntime finally blocks must never erase a transaction.
        # Managed or inherited stores require verified clear_proxy_snapshot/RT-1.
        # Retain explicit save-without-owner + clear compatibility for old users.
        if self.proxy_transactions_managed or self._legacy_clear_expected is None:
            return
        assert_proxy_journal_writer()
        try:
            if self.load_proxy_journal().get("system_proxy") != self._legacy_clear_expected:
                return
            self.path.unlink()
        except FileNotFoundError:
            return


def build_restore_proxy_script(path: str | Path = "restore_proxy.bat") -> None:
    script = """@echo off
reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f
reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings" /v ProxyServer /t REG_SZ /d "" /f
reg add "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings" /v ProxyOverride /t REG_SZ /d "" /f
echo Current-user system proxy disabled.
pause
"""
    Path(path).write_text(script, encoding="utf-8")
