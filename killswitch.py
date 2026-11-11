from __future__ import annotations

import atexit
from collections import deque
from dataclasses import dataclass
import ctypes
import inspect
import ipaddress
import logging
import platform
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from config_utils import coerce_bool, coerce_float
from log_event_reports import log_raw_diagnostic
from windows_subprocess import hidden_subprocess_kwargs


KILL_SWITCH_RULE_NAMES = {
    "block": "ProxyManager-KillSwitch-Block",
    "localhost": "ProxyManager-KillSwitch-Allow-Localhost",
    "lan": "ProxyManager-KillSwitch-Allow-LAN",
    "tier1": "ProxyManager-KillSwitch-Allow-Tier1",
    "additional": "ProxyManager-KillSwitch-Allow-Additional",
    "dns": "ProxyManager-KillSwitch-Allow-DNS",
    "core_program": "ProxyManager-KillSwitch-Allow-Core",
}


DEFAULT_LAN_RANGES = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
DEFAULT_FIREWALL_PROFILES = ["private", "public"]

_LOGGER = logging.getLogger("proxy_manager.killswitch")


class FirewallWritesRevoked(RuntimeError):
    pass


class FirewallAuthority:
    """Process lifetime authority, including managers constructed after RT-2."""
    def __init__(self):
        from runtime_session_end import RecoveryBarrier
        self.condition = threading.Condition()
        self.revoked = False
        self.credential = None
        self.in_flight = 0
        self.recovering = False
        self.gate = RecoveryBarrier()


_FIREWALL_AUTHORITY = FirewallAuthority()


def revoke_killswitch_writes(manager=None):
    authority = _FIREWALL_AUTHORITY
    with authority.condition:
        authority.revoked = True
        authority.credential = object()
        credential = authority.credential
    if manager is not None:
        # The command/lifecycle lock may be held across netsh. Only the short
        # event lock is allowed here; P2-20 drains the same sequence ring.
        with manager._event_lock:
            if manager._events is None:
                manager._events = deque(maxlen=128)
            manager._record_state_event("writes_revoked")
    return credential


def emergency_restore_firewall(credential, *, manager=None, deadline):
    from runtime_network_recovery import recover_startup_firewall
    from runtime_session_end import domain
    authority = _FIREWALL_AUTHORITY
    if not authority.gate.enter():
        return domain(code="recovery_closed")
    acquired = False
    recovering = False
    try:
        with authority.condition:
            if (not authority.revoked or credential is None
                    or credential is not authority.credential):
                return domain(code="writes_revoked")
            if authority.recovering:
                return domain(code="recovery_in_progress")
            authority.credential = None
            authority.recovering = recovering = True
            if not authority.condition.wait_for(lambda: not authority.in_flight,
                    max(0., deadline - time.monotonic())):
                return domain()
        if manager is not None:
            acquired = manager.lock.acquire(timeout=max(0., deadline - time.monotonic()))
            if not acquired:
                return domain()
        # This firewall-only API is synchronous and verifies exact owned rule
        # identities. RT-1's proxy/journal phase is never entered here.
        result = recover_startup_firewall(
            may_take_over_host_network=True,
            deadline_seconds=max(0., deadline - time.monotonic()),
            command_runner=recovery_command_runner,
            admin_available=manager.admin_available if manager is not None else None)
        if manager is not None:
            if result["state"] in {"restored", "not_needed"}:
                manager.active = False
                manager.manual_override = "none"
                manager.state = manager._initial_state()
                manager.last_error = None
            else:
                manager.last_error = result["error_code"]
            manager._record_state_event("emergency_restore")
        return result
    finally:
        if acquired:
            manager.lock.release()
        if recovering:
            with authority.condition:
                authority.recovering = False
        authority.gate.leave()

# Whitelist editors and hand-edited configs may put several entries on one
# line: whitespace, "," ";" and their full-width forms, or the ideographic
# comma all separate entries.
WHITELIST_ENTRY_SEPARATORS = re.compile(r"[\s,;，；、]+")

# netsh advfirewall remoteip= keywords accepted in the additional whitelist.
# "any" is deliberately not accepted: it would allow all outbound traffic.
FIREWALL_REMOTE_KEYWORDS = {
    "localsubnet": "LocalSubnet",
    "dns": "DNS",
    "dhcp": "DHCP",
    "wins": "WINS",
    "defaultgateway": "DefaultGateway",
}


def firewall_ip_token(value: Any) -> str | None:
    """Return the netsh form of one IP address or subnet, or None."""
    text = str(value or "").strip()
    if not text or "%" in text:
        return None
    try:
        if "/" in text:
            return str(ipaddress.ip_network(text, strict=False))
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def firewall_remote_token(value: Any) -> str | None:
    """Return the netsh remoteip= form of one additional-whitelist entry.

    Accepts an IPv4/IPv6 address, a subnet (CIDR or address/mask), an a-b
    address range within one IP version, and the keywords LocalSubnet, DNS,
    DHCP, WINS and DefaultGateway. Anything else returns None, because one
    invalid item makes netsh reject the whole allow rule.
    """
    text = str(value or "").strip()
    keyword = FIREWALL_REMOTE_KEYWORDS.get(text.lower())
    if keyword:
        return keyword
    if "-" in text:
        start_text, _, end_text = text.partition("-")
        start = firewall_ip_token(start_text)
        end = firewall_ip_token(end_text)
        if start is None or end is None or "/" in text:
            return None
        start_address = ipaddress.ip_address(start)
        end_address = ipaddress.ip_address(end)
        if start_address.version != end_address.version or start_address > end_address:
            return None
        return f"{start}-{end}"
    return firewall_ip_token(text)


def build_unkill_script(path: str | Path = "unkill.bat") -> None:
    delete_lines = "\n".join(
        f'netsh advfirewall firewall delete rule name="{rule_name}"'
        for rule_name in KILL_SWITCH_RULE_NAMES.values()
    )
    script = (
        "@echo off\n"
        f"{delete_lines}\n"
        "echo Kill switch rules removed.\n"
        "pause\n"
    )
    Path(path).write_text(script, encoding="utf-8")


@dataclass(frozen=True, slots=True)
class KillSwitchEvent:
    ks_seq: int
    reason: str
    enabled: bool
    active: bool
    admin: bool
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class KillSwitchGap:
    from_seq: int
    to_seq: int


@dataclass(frozen=True, slots=True)
class KillSwitchDrain:
    events: tuple[KillSwitchEvent, ...]
    snapshot: KillSwitchEvent
    gap: KillSwitchGap | None = None


class KillSwitchManager:
    def __init__(
        self,
        config: dict[str, Any],
        tier1_nodes: list[dict[str, Any]],
        command_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        core_program_path: str | None = None,
        *,
        event_capacity: int = 0,
    ) -> None:
        if type(event_capacity) is not int or event_capacity < 0:
            raise ValueError("event_capacity must be a nonnegative integer")
        self._apply_config(config)
        self.tier1_endpoints = [str(node.get("address", "")).strip() for node in tier1_nodes if node.get("address")]
        self.tier1_addresses = self._resolve_tier1_addresses()
        self.command_runner = command_runner or self._default_command_runner
        self.platform_name = platform.system().lower()
        self.admin_available = self._detect_admin()
        self.manual_override = "none"
        self.active = False
        self.last_error: str | None = None
        self.lock = threading.RLock()
        self._event_lock = threading.RLock()
        self.core_program_path = core_program_path
        self._last_command_error_monotonic = 0.0
        self._last_tier1_resolution_monotonic = time.monotonic()
        self.state = self._initial_state()
        # Qt's default path owns no ring and never allocates event records.
        self._events: deque[KillSwitchEvent] | None = (
            deque(maxlen=event_capacity) if event_capacity else None
        )
        self._ks_seq = 0
        self._dropped_through = 0
        atexit.register(self.cleanup)

    def _event_snapshot(self, reason: str) -> KillSwitchEvent:
        # Called under _event_lock; never copy raw last_error or firewall data.
        return KillSwitchEvent(
            self._ks_seq, reason, self.enabled, self.active,
            self.admin_available, "io" if self.last_error is not None else None,
        )

    def _record_state_event(self, reason: str) -> None:
        with self._event_lock:
            # Ordinary callers also own self.lock; RT-2 revocation needs only
            # this short event boundary. No sink is invoked here.
            if self._events is None:
                return
            self._ks_seq += 1
            if len(self._events) == self._events.maxlen:
                self._dropped_through = self._events[0].ks_seq
            self._events.append(self._event_snapshot(reason))

    def snapshot_with_seq(self) -> KillSwitchEvent:
        """Atomic minimal state and source watermark, including when disabled."""
        with self.lock, self._event_lock:
            return self._event_snapshot("snapshot")

    def drain(self, after_seq: int) -> KillSwitchDrain:
        """Read a bounded tail without deleting it; independent cursors are safe.

        The gap is the evicted prefix intersected with this caller's unread
        interval. Snapshot and tail share one lock acquisition. A cursor from
        another manager lifetime is rejected rather than silently skipping.
        """
        with self.lock, self._event_lock:
            if type(after_seq) is not int or not 0 <= after_seq <= self._ks_seq:
                raise ValueError("invalid killswitch cursor")
            gap = (KillSwitchGap(after_seq + 1, self._dropped_through)
                   if after_seq < self._dropped_through else None)
            events = tuple(event for event in self._events or ()
                           if event.ks_seq > after_seq)
            return KillSwitchDrain(events, self._event_snapshot("snapshot"), gap)

    def _initial_state(self) -> str:
        if not self.enabled:
            return "disabled"
        if self.platform_name != "windows":
            return "disabled"
        if not self.admin_available:
            return "disabled"
        return "armed"

    def _apply_config(self, config: dict[str, Any]) -> None:
        self.config = config
        # Off unless the configuration turns it on (the shipped default is off).
        self.enabled = coerce_bool(config.get("enabled", False), False)
        self.allow_lan = coerce_bool(config.get("allow_lan", True), True)
        self.additional_whitelist = self._firewall_remote_whitelist(
            self._normalize_whitelist(config.get("additional_whitelist", []))
        )
        self.dns_whitelist = self._normalize_whitelist(config.get("dns_whitelist", []))
        self.allow_dns = coerce_bool(config.get("allow_dns", bool(self.dns_whitelist)), bool(self.dns_whitelist))
        self.profiles = self._normalize_profiles(config.get("profiles", DEFAULT_FIREWALL_PROFILES))
        self.activation_retry_interval = coerce_float(
            config.get("activation_retry_interval", 30),
            30.0,
            minimum=1.0,
        )
        self.dynamic_whitelist_refresh_interval = coerce_float(
            config.get("dynamic_whitelist_refresh_interval", 300),
            300.0,
            minimum=30.0,
        )

    def reconfigure(
        self,
        config: dict[str, Any],
        tier1_nodes: list[dict[str, Any]],
        core_program_path: str | None = None,
    ) -> None:
        with self.lock:
            if _FIREWALL_AUTHORITY.revoked:
                return
            was_active = self.active
            was_forced = self.manual_override == "forced_on"
            next_enabled = coerce_bool(config.get("enabled", False), False)
            if was_active or not next_enabled:
                self._cleanup_rules()
            self._apply_config(config)
            self.tier1_endpoints = [str(node.get("address", "")).strip() for node in tier1_nodes if node.get("address")]
            self.tier1_addresses = self._resolve_tier1_addresses()
            self._last_tier1_resolution_monotonic = time.monotonic()
            self.admin_available = self._detect_admin()
            self.manual_override = "none"
            self.active = False
            if core_program_path is not None:
                self.core_program_path = core_program_path
            if not self.enabled:
                self.last_error = None
            self.state = self._initial_state()
            self._record_state_event("reconfigure")
            if self.state == "disabled":
                return
            if was_forced:
                self.force_on()
            elif was_active:
                self.activate()

    def _detect_admin(self) -> bool:
        if not self.enabled:
            return False
        if self.platform_name != "windows":
            return False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def is_admin(self) -> bool:
        return self.admin_available

    def _default_command_runner(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )

    def _run_command(self, command: list[str], *, timeout: float | None = None):
        # Final OS checkpoint: no manager, late activate or reconfigure can
        # bypass the process-wide revocation by retaining an old instance.
        authority = _FIREWALL_AUTHORITY
        with authority.condition:
            if authority.revoked:
                raise FirewallWritesRevoked("writes_revoked")
            authority.in_flight += 1
        try:
            return self._run_authorized_command(command, timeout=timeout)
        finally:
            with authority.condition:
                authority.in_flight -= 1
                authority.condition.notify_all()

    def _run_authorized_command(
        self,
        command: list[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        runner = self.command_runner
        if timeout is not None:
            try:
                parameters = inspect.signature(runner).parameters.values()
            except (TypeError, ValueError):
                parameters = ()
            if any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                or parameter.name == "timeout"
                for parameter in parameters
            ):
                return runner(command, timeout=max(0.0, float(timeout)))
        return runner(command)

    def _normalize_whitelist(self, value: Any) -> list[str]:
        if isinstance(value, str):
            raw_values = [value]
        elif isinstance(value, (list, tuple, set)):
            raw_values = list(value)
        else:
            raw_values = []
        entries: list[str] = []
        for item in raw_values:
            for text in WHITELIST_ENTRY_SEPARATORS.split(str(item or "")):
                if text and text not in entries:
                    entries.append(text)
        return entries

    def _firewall_remote_whitelist(self, entries: list[str]) -> list[str]:
        accepted: list[str] = []
        rejected: list[str] = []
        for entry in entries:
            token = firewall_remote_token(entry)
            if token is None:
                rejected.append(entry)
            elif token not in accepted:
                accepted.append(token)
        if rejected:
            log_raw_diagnostic(
                _LOGGER,
                "warning",
                "Kill switch ignored invalid additional_whitelist entries: %s",
                ", ".join(rejected),
            )
        return accepted

    def _normalize_profiles(self, value: Any) -> list[str]:
        if isinstance(value, str):
            raw_profiles = [part.strip() for part in value.split(",")]
        elif isinstance(value, list):
            raw_profiles = [str(part).strip() for part in value]
        else:
            raw_profiles = list(DEFAULT_FIREWALL_PROFILES)
        allowed = {"domain", "private", "public", "any"}
        profiles = []
        for item in raw_profiles:
            normalized = item.lower()
            if normalized in allowed and normalized not in profiles:
                profiles.append(normalized)
        return profiles or list(DEFAULT_FIREWALL_PROFILES)

    def _profile_arg(self) -> str | None:
        if not self.profiles or "any" in self.profiles:
            return None
        return f"profile={','.join(self.profiles)}"

    def _delete_rule(self, rule_name: str, *, timeout: float | None = None) -> None:
        self._run_command(
            ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule_name}"],
            timeout=timeout,
        )

    def _add_rule(self, *parts: str) -> None:
        command_parts = list(parts)
        profile_arg = self._profile_arg()
        if profile_arg and not any(part.lower().startswith("profile=") for part in command_parts):
            command_parts.append(profile_arg)
        self._run_command(["netsh", "advfirewall", "firewall", "add", "rule", *command_parts])

    def _managed_rule_names(self) -> list[str]:
        return list(KILL_SWITCH_RULE_NAMES.values())

    def _is_firewall_address(self, value: str) -> bool:
        return firewall_ip_token(value) is not None

    def _resolve_tier1_addresses(self) -> list[str]:
        resolved: set[str] = set()
        for endpoint in self.tier1_endpoints:
            value = endpoint.strip()
            if not value:
                continue
            if self._is_firewall_address(value):
                resolved.add(value)
                continue
            try:
                for item in socket.getaddrinfo(value, None, type=socket.SOCK_STREAM):
                    address = str(item[4][0]).strip()
                    if address and self._is_firewall_address(address):
                        resolved.add(address)
            except OSError:
                continue
        return sorted(resolved)

    def _refresh_tier1_addresses_if_due(self) -> bool:
        now = time.monotonic()
        if (now - self._last_tier1_resolution_monotonic) < self.dynamic_whitelist_refresh_interval:
            return False
        self._last_tier1_resolution_monotonic = now
        addresses = self._resolve_tier1_addresses()
        if not addresses or addresses == self.tier1_addresses:
            return False
        self.tier1_addresses = addresses
        return True

    def _dns_addresses_csv(self) -> str:
        if not self.allow_dns:
            return ""
        return ",".join(sorted(set(address for address in self.dns_whitelist if self._is_firewall_address(address))))

    def _refresh_active_tier1_rule_if_due(self) -> None:
        if not self.active or not self._refresh_tier1_addresses_if_due():
            return
        tier1_ips = self._tier1_addresses_csv()
        if not tier1_ips:
            return
        self._delete_rule(KILL_SWITCH_RULE_NAMES["tier1"])
        self._add_rule(
            f'name={KILL_SWITCH_RULE_NAMES["tier1"]}',
            "dir=out",
            "action=allow",
            f"remoteip={tier1_ips}",
            "enable=yes",
        )

    def _local_addresses(self) -> str:
        return "127.0.0.1,::1"

    def _tier1_addresses_csv(self) -> str:
        return ",".join(sorted(set(address for address in self.tier1_addresses if address)))

    def _additional_addresses_csv(self) -> str:
        return ",".join(sorted(set(address for address in self.additional_whitelist if address)))

    def _cleanup_rules(self, *, timeout: float | None = None) -> None:
        if _FIREWALL_AUTHORITY.revoked:
            return
        deadline = (
            time.monotonic() + max(0.0, float(timeout))
            if timeout is not None
            else None
        )
        for rule_name in self._managed_rule_names():
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining is not None and remaining <= 0.0:
                break
            try:
                self._delete_rule(rule_name, timeout=remaining)
            except Exception:
                continue

    def _ensure_supported(self) -> bool:
        if _FIREWALL_AUTHORITY.revoked:
            return False
        if self.state == "disabled":
            return False
        if (
            self.state == "error"
            and self._last_command_error_monotonic
            and (time.monotonic() - self._last_command_error_monotonic) < self.activation_retry_interval
        ):
            return False
        if not self.enabled or self.platform_name != "windows" or not self.admin_available:
            self.state = "disabled"
            self.active = False
            self._record_state_event("disabled")
            return False
        return True

    def _record_command_error(self, exc: Exception) -> None:
        self.last_error = str(exc)
        self.active = False
        self.manual_override = "none"
        self.state = "error"
        self._last_command_error_monotonic = time.monotonic()
        self._record_state_event("error")

    def activate(self) -> bool:
        with self.lock:
            if not self._ensure_supported():
                return False
            if self.active:
                if self.state != "triggered":
                    self.state = "triggered"
                    self._record_state_event("activate")
                try:
                    self._refresh_active_tier1_rule_if_due()
                except Exception as exc:
                    self._record_command_error(exc)
                return False
            try:
                self._cleanup_rules()
                self.tier1_addresses = self._resolve_tier1_addresses()
                self._last_tier1_resolution_monotonic = time.monotonic()
                self._add_rule(
                    f'name={KILL_SWITCH_RULE_NAMES["localhost"]}',
                    "dir=out",
                    "action=allow",
                    f"remoteip={self._local_addresses()}",
                    "enable=yes",
                )
                if self.allow_lan:
                    self._add_rule(
                        f'name={KILL_SWITCH_RULE_NAMES["lan"]}',
                        "dir=out",
                        "action=allow",
                        f"remoteip={','.join(DEFAULT_LAN_RANGES)}",
                        "enable=yes",
                    )
                tier1_ips = self._tier1_addresses_csv()
                if tier1_ips:
                    self._add_rule(
                        f'name={KILL_SWITCH_RULE_NAMES["tier1"]}',
                        "dir=out",
                        "action=allow",
                        f"remoteip={tier1_ips}",
                        "enable=yes",
                    )
                additional_ips = self._additional_addresses_csv()
                if additional_ips:
                    self._add_rule(
                        f'name={KILL_SWITCH_RULE_NAMES["additional"]}',
                        "dir=out",
                        "action=allow",
                        f"remoteip={additional_ips}",
                        "enable=yes",
                    )
                dns_ips = self._dns_addresses_csv()
                if dns_ips:
                    self._add_rule(
                        f'name={KILL_SWITCH_RULE_NAMES["dns"]}',
                        "dir=out",
                        "action=allow",
                        "protocol=udp",
                        "remoteport=53",
                        f"remoteip={dns_ips}",
                        "enable=yes",
                    )
                if self.core_program_path:
                    # In TUN mode the mihomo process is the one sourcing all
                    # outbound traffic. Allowing the program itself keeps TUN
                    # traffic flowing while still blocking every other app.
                    self._add_rule(
                        f'name={KILL_SWITCH_RULE_NAMES["core_program"]}',
                        "dir=out",
                        "action=allow",
                        f"program={self.core_program_path}",
                        "enable=yes",
                    )
                self._add_rule(
                    f'name={KILL_SWITCH_RULE_NAMES["block"]}',
                    "dir=out",
                    "action=block",
                    "enable=yes",
                )
            except Exception as exc:
                self._cleanup_rules()
                self._record_command_error(exc)
                return False
            self.active = True
            self.state = "triggered"
            self.last_error = None
            self._last_command_error_monotonic = 0.0
            self._record_state_event("activate")
            return True

    def deactivate(self) -> bool:
        with self.lock:
            if not self._ensure_supported():
                return False
            if not self.active:
                if self.state != "armed":
                    self.state = "armed"
                    self._record_state_event("deactivate")
                return False
            try:
                self._cleanup_rules()
            except Exception as exc:
                self._record_command_error(exc)
                return False
            self.active = False
            self.state = "armed"
            self.last_error = None
            self._record_state_event("deactivate")
            return True

    def force_on(self) -> bool:
        with self.lock:
            if not self._ensure_supported():
                return False
            already_forced = self.manual_override == "forced_on" and self.active
            changed = self.activate()
            if self.state == "disabled":
                return False
            record_force_on = (
                self._events is not None and (changed
                    or self.manual_override != "forced_on" or self.state != "triggered")
            )
            self.manual_override = "forced_on"
            self.state = "triggered"
            if record_force_on:
                self._record_state_event("force_on")
            return changed or not already_forced

    def force_off(self) -> bool:
        with self.lock:
            had_override = self.manual_override == "forced_on"
            if not self._ensure_supported():
                self.manual_override = "none"
                if had_override:
                    self._record_state_event("force_off")
                return had_override
            changed = self.deactivate()
            self.manual_override = "none"
            self.state = "armed"
            self._record_state_event("force_off")
            return changed or had_override

    def cleanup_stale_rules(self) -> bool:
        with self.lock:
            if not self._ensure_supported():
                return False
            try:
                self._cleanup_rules()
            except Exception as exc:
                self._record_command_error(exc)
                return False
            if not self.active:
                self.state = "armed"
            self.last_error = None
            self._record_state_event("cleanup_stale_rules")
            return True

    def cleanup(self, timeout: float = 2.0) -> None:
        if _FIREWALL_AUTHORITY.revoked:
            return
        if self.platform_name != "windows":
            return
        deadline = time.monotonic() + max(0.0, float(timeout))
        acquired = self.lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not acquired:
            return
        try:
            self._cleanup_rules(timeout=max(0.0, deadline - time.monotonic()))
            self.active = False
            self.manual_override = "none"
            if self.enabled and self.admin_available:
                self.state = "armed"
            self._record_state_event("cleanup")
        finally:
            self.lock.release()

    def close(self, timeout: float = 2.0) -> None:
        """Run the final bounded cleanup and suppress a duplicate atexit pass."""
        try:
            atexit.unregister(self.cleanup)
        except Exception:
            pass
        # Startup removes stale managed rules before the runtime is exposed.
        # If this instance never activated and is not recovering from an error,
        # there are no session-owned rules to remove. Avoid launching seven
        # redundant netsh processes in the interpreter/Qt shutdown window.
        with self.lock:
            cleanup_required = bool(
                self.active
                or self.manual_override == "forced_on"
                or self.state in {"triggered", "error"}
            )
        if not cleanup_required:
            return
        self.cleanup(timeout=timeout)

    def get_state(self) -> dict[str, Any]:
        with self.lock:
            return {
                "state": self.state,
                "manual_override": self.manual_override,
                "active": self.active,
                "admin_available": self.admin_available,
                "enabled": self.enabled,
                "supported": self.platform_name == "windows",
                "last_error": self.last_error,
                "whitelist": {
                    "localhost": [ip.strip() for ip in self._local_addresses().split(",")],
                    "lan": list(DEFAULT_LAN_RANGES) if self.allow_lan else [],
                    "tier1": sorted(set(self.tier1_addresses)),
                    "additional": sorted(set(self.additional_whitelist)),
                    "dns": sorted(set(self.dns_whitelist)) if self.allow_dns else [],
                    "dns_udp_53": bool(self._dns_addresses_csv()),
                    "profiles": list(self.profiles),
                },
            }


def recovery_command_runner(
    command: list[str], *, timeout: float, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one recovery command; subprocess.run kills and waits on timeout."""
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, env=env, **hidden_subprocess_kwargs(),
    )


# The shipped runner object, captured before any test or audit replaces the
# module attribute: only this runner means "the real host" to the inventory.
_SHIPPED_RECOVERY_RUNNER = recovery_command_runner

# Windows Firewall's persistent rule store: one REG_SZ value per rule, the
# value name is the rule ID (Get-NetFirewallRule .Name, a GUID for netsh
# rules) and the data is "v2.xx|Action=..|..|Name=<DisplayName>|..".
# BUILTIN\Users may read it.
_FIREWALL_RULES_KEY = (
    r"SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules"
)
_RECOVERY_RULE_PREFIX = "ProxyManager-KillSwitch-"


def _registry_rule_display_name(data: object) -> str | None:
    if not isinstance(data, str):
        return None
    for field in data.split("|"):
        if field.startswith("Name="):
            return field[5:]
    return None


def registry_firewall_rules(*, timeout: float) -> list[dict[str, str]]:
    """Candidate rules from the persistent store in-process (milliseconds).

    Same rows as the PowerShell inventory: the prefix match is
    case-insensitive like ``-like``. Raises OSError when the store cannot be
    read; the caller then falls back to PowerShell.
    """
    if timeout <= 0:
        raise TimeoutError("firewall_deadline_exceeded")
    import winreg
    prefix = _RECOVERY_RULE_PREFIX.casefold()
    rows: list[dict[str, str]] = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _FIREWALL_RULES_KEY, 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        count = winreg.QueryInfoKey(key)[1]
        for index in range(count):
            name, data, _kind = winreg.EnumValue(key, index)
            display = _registry_rule_display_name(data)
            if display is not None and display.casefold().startswith(prefix):
                rows.append({"name": name, "display_name": display})
    return rows


def recovery_firewall_rules(
    command_runner: Callable[..., subprocess.CompletedProcess[str]], *, timeout: float,
) -> list[dict[str, str]]:
    """Read candidate rules without constructing a KillSwitchManager.

    The broad prefix only collects diagnostics. Callers must require an exact
    DisplayName from KILL_SWITCH_RULE_NAMES before deleting a rule by its ID.

    With the shipped runner the persistent store is read from the registry
    first: spawning PowerShell and listing every rule took ~1.6 s warm and
    more on a cold first launch, which overran the 2 s startup budget and
    showed "firewall: not confirmed" on a clean install (11-10). An injected
    runner (tests, audits) is served through PowerShell as before.
    """
    if command_runner is _SHIPPED_RECOVERY_RUNNER and platform.system() == "Windows":
        try:
            return registry_firewall_rules(timeout=timeout)
        except OSError:
            pass
    script = (
        "$ErrorActionPreference='Stop'; try { "
        "ConvertTo-Json -InputObject @(Get-NetFirewallRule -ErrorAction Stop | Where-Object { "
        "$_.DisplayName -like 'ProxyManager-KillSwitch-*' } | ForEach-Object { "
        "@{ name=$_.Name; display_name=$_.DisplayName } }) "
        "-Compress -Depth 3 } catch { exit 1 }"
    )
    result = command_runner(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError("firewall_inventory_failed")
    raw = result.stdout.strip()
    if not raw:
        raise ValueError("firewall_inventory_empty")
    import json
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("firewall_inventory_invalid")
    if any(not isinstance(item, dict)
           or not isinstance(item.get("name"), str)
           or not isinstance(item.get("display_name"), str)
           for item in value):
        raise ValueError("firewall_inventory_invalid")
    return value


_RECOVERY_RULE_GUID = re.compile(
    r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}"
)
_RECOVERY_DELETE_SCRIPT = (
    "$ErrorActionPreference='Stop'; try { "
    "$identity=$env:TESSERACT_RECOVERY_RULE_IDENTITY | ConvertFrom-Json -ErrorAction Stop; "
    "$selected=@(Get-NetFirewallRule -ErrorAction Stop | Where-Object { "
    "[string]::Equals($_.Name, $identity.rule_id, [StringComparison]::Ordinal) -and "
    "[string]::Equals($_.DisplayName, $identity.display_name, [StringComparison]::Ordinal) }); "
    "if ($selected.Count -ne 1) { exit 1 }; "
    "Remove-NetFirewallRule -InputObject $selected[0] -ErrorAction Stop "
    "} catch { exit 1 }"
)


def recovery_rule_identity_valid(rule_id: str, display_name: str) -> bool:
    """Require a Windows rule GUID and an ordinally equal owned display name."""
    return (
        isinstance(rule_id, str)
        and _RECOVERY_RULE_GUID.fullmatch(rule_id) is not None
        and isinstance(display_name, str)
        and display_name in KILL_SWITCH_RULE_NAMES.values()
    )


def recovery_delete_owned_firewall_rule(
    rule_id: str, display_name: str,
    command_runner: Callable[..., subprocess.CompletedProcess[str]], *, timeout: float,
) -> None:
    """Remove one freshly selected rule object after exact ID and name checks.

    The fixed script reads validated identity data from JSON in the environment
    and checks the current rule object again immediately before deletion.
    """
    if not recovery_rule_identity_valid(rule_id, display_name):
        raise ValueError("firewall_rule_identity_invalid")
    import json
    import os
    env = os.environ.copy()
    env["TESSERACT_RECOVERY_RULE_IDENTITY"] = json.dumps(
        {"rule_id": rule_id, "display_name": display_name}, ensure_ascii=True,
    )
    result = command_runner(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _RECOVERY_DELETE_SCRIPT],
        timeout=timeout, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError("firewall_delete_failed")
