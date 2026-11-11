"""Update owner for one sidecar epoch; integration supplies ordinary execution.

No Qt, runtime construction, timer thread, installer launch or transport I/O.
Call on_ready once, poll_auto_check from the existing bridge tick, register the
two handlers, and publish take_state()/snapshot() through the topic publisher.
Notice candidates are local inputs to P2-22, never proof of delivery.

P2-27 adds the shell-only install handoff (PROTOCOL §11/§13): a verified
receipt yields a short-lived handoff published as update.prepareExit, the shell
answers update.exitResponse, and after a failed launch update.installResult.
Python never launches the installer and never requests the exit: accepted=true
only records the takeover, the shell itself sends app.quit{reason:update}
(PROTOCOL §13), and a read-only port lets the tick observe that exit began.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from copy import deepcopy
import logging
import os
import re
from pathlib import Path
from threading import RLock
from time import monotonic, time_ns
from typing import Any, Callable, Protocol
from uuid import uuid4

import app_updater
import app_update_state
from app_info import APP_UPDATE_ENABLED, APP_VERSION
from bridge_jobs import JobFailure


TOPIC = "update.state"
OWNER = "update_owner"
AUTO_CHECK_DELAY_SECONDS = 6.0
HANDOFF_TOPIC = "update.prepareExit"
HANDOFF_SECONDS = 30.0  # PROTOCOL §13 default shell confirmation window.
# After accepted=true the shell must send app.quit{reason:update}; if no exit
# has begun within this window the takeover is withdrawn (verified/lifecycle_busy).
EXIT_START_SECONDS = 30.0
# Exactly the switches app_updater.launch_update_installer passes after the
# installer path (argv[1:]); the shell launches installer_path with these.
INSTALLER_ARGS = ("/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS",
                  "/RESTARTAPPLICATIONS", "/NORESTART")
_LOG = logging.getLogger(__name__)


class HandoffPublisher(Protocol):
    """Publish one shell-only update.prepareExit edge; raise if not accepted."""

    def publish_prepare_exit(self, data: dict[str, Any]) -> None: ...


class UpdateExitPort(Protocol):
    """Read-only view of the exit coordinator (PROTOCOL §13).

    The shell, not the sidecar, sends app.quit{reason:update} after its
    accepted exitResponse was recorded. exit_started() reports whether this
    sidecar's orderly exit has begun (permanent once True). MUST be a
    non-blocking read: no submit, no waiting, no mutation-domain work.
    """

    def exit_started(self) -> bool: ...


class StateHandoffPublisher:
    """Adapter onto the existing PublicationState edge API (record_edge)."""

    def __init__(self, state: Any) -> None:
        self._state = state

    def publish_prepare_exit(self, data: dict[str, Any]) -> None:
        self._state.record_edge(HANDOFF_TOPIC, data)


class ShutdownExitAdapter:
    """Adapter onto the public ShutdownCoordinator.quitting property.

    Never calls request_quit(): a sidecar-initiated quit inside the control
    job would end that very exitResponse job as runtime_gone. The shell's
    app.quit{reason:update} reaches the coordinator through the bridge ingress,
    which carries the reason in the job arguments.
    """

    def __init__(self, coordinator: Callable[[], Any]) -> None:
        self._coordinator = coordinator

    def exit_started(self) -> bool:
        return bool(self._coordinator().quitting)


MAX_NOTES_CHARS = 8000
_URL_SCHEME = re.compile(r"(?i)(?<![a-z0-9])([a-z][a-z0-9+.-]{0,63})://([^\s/?#]*)")
_LINK_SCHEMES = frozenset({"http", "https"})
_NOTE_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u2028\u2029]")


def plain_release_notes(text: Any) -> str | None:
    """T-P4-C2 #10: ``Update.notes`` - release notes as plain, untrusted text.

    The in-app section of the release body (``extract_app_release_notes``),
    with control characters removed, link schemes dropped (``https://x/y`` ->
    ``x/y``; the page renders text, never links or HTML) and at most
    MAX_NOTES_CHARS characters (longer notes end with an ellipsis). Notes the
    frame scanner would still reject are not sent (None), so a release body
    can never invalidate the update.state topic. Empty -> None.

    Only plain ``http``/``https`` links lose their scheme. Any other scheme
    (a node URI such as ``vless://uuid@host``) or a link with userinfo makes
    the whole notes None, judged before stripping: dropping the scheme first
    would let ``uuid@host:443`` past the frame scanner.
    """
    from bridge_protocol import contains_sensitive_text
    notes = _NOTE_CONTROL.sub("", str(text or "").replace("\r\n", "\n").replace("\r", "\n"))
    unsafe = False

    def drop_scheme(match: re.Match[str]) -> str:
        nonlocal unsafe
        if match.group(1).lower() not in _LINK_SCHEMES or "@" in match.group(2):
            unsafe = True
        return match.group(2)
    notes = _URL_SCHEME.sub(drop_scheme, notes).strip()
    if unsafe:
        return None
    if len(notes) > MAX_NOTES_CHARS:
        notes = notes[:MAX_NOTES_CHARS - 1].rstrip() + "…"
    if not notes or contains_sensitive_text(notes):
        return None
    return notes


class UpdateOwner:
    """All checks share a flight; checks and download preparation cannot overlap.

    The check/download callables are blocking and MUST run in the normal domain.
    State/candidate readers only take a short owner lock. A separate persistence
    lock serializes state-file read/modify/write operations in ordinary workers.
    Never perform disk I/O while holding the owner lock.
    """

    def __init__(
        self, state_path: Path, download_dir: Path, *,
        current_version: str = APP_VERSION,
        updater: Any = app_updater,
        clock: Callable[[], float] = monotonic,
        wall_ms: Callable[[], int] = lambda: time_ns() // 1_000_000,
        handoff_seconds: float = HANDOFF_SECONDS,
        exit_start_seconds: float = EXIT_START_SECONDS,
        update_enabled: bool = APP_UPDATE_ENABLED,
    ) -> None:
        self.state_path = Path(state_path)
        self.download_dir = Path(download_dir)
        self.current_version = current_version
        self._updater = updater
        self._clock = clock
        self._lock = RLock()
        self._persistence_lock = RLock()
        self._flight: Future | None = None
        self._installing = False
        self._auto_due: float | None = None
        self._auto_started = False
        self._info: app_updater.AppUpdateInfo | None = None
        self._state = "idle"
        self._error: dict[str, Any] | None = None
        self._dirty = True
        self._receipt_ref: str | None = None
        self._deliveries: dict[str, str] = {}
        self._candidates: deque[dict[str, str]] = deque()
        self._offered: deque[str] = deque(maxlen=16)
        self._wall_ms = wall_ms
        self._handoff_seconds = float(handoff_seconds)
        self._exit_start_seconds = float(exit_start_seconds)
        self._publish_handoff: HandoffPublisher | None = None
        self._exit_port: UpdateExitPort | None = None
        # {"id", "receipt_ref", "deadline", "responded", "exit_deadline"};
        # at most one handoff.
        self._handoff: dict[str, Any] | None = None
        # Exit observed after an accepted handoff: permanent for this epoch.
        self._exit_observed = False
        self._last_install_result: dict[str, Any] | None = None
        # T-P4-C1 #7 (G51): the version this sidecar replaced, when the app
        # was just updated to current_version (see load_completed_update).
        self._previous_version: str | None = None
        # T-P4-C2 #10 (settings R-09): build switch and last finished check.
        self._update_enabled = bool(update_enabled)
        self._last_check_at_ms: int | None = None

    def _dto(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "version": app_updater.normalize_version(self._info.latest_version) if self._info else None,
            "update_available": bool(self._info and self._info.update_available),
            "installable": bool(self._info and self._info.installable),
            "error": deepcopy(self._error),
            "current_version": app_updater.normalize_version(self.current_version) or None,
            "previous_version": self._previous_version,
            "notes": plain_release_notes(self._info.release_notes)
            if self._info and self._info.update_available else None,
            "enabled": self._update_enabled,
            "last_check_at_ms": self._last_check_at_ms,
        }

    def _transition(self, state: str, code: str | None = None) -> None:
        self._state = state
        self._error = (None if code is None else {
            "code": code, "message_key": "errors.update.operation_failed", "params": {},
        })
        self._dirty = True

    def snapshot(self) -> dict[str, Any]:
        """Unstamped topic template; publisher owns epoch/rev/time/sequence."""
        with self._lock:
            return {"topic": TOPIC, "kind": "snapshot", "data": self._dto()}

    def take_state(self) -> dict[str, Any] | None:
        """Pull the latest coalesced state into a reliable publisher queue.

        Consumer must retain/retry the returned template if enqueue fails.
        snapshot() always remains available, including after reminder success.
        """
        with self._lock:
            if not self._dirty:
                return None
            self._dirty = False
            return self.snapshot()

    def load_completed_update(self) -> str | None:
        """Read once at startup whether this run is a just-completed update.

        Disk I/O (the update state file), outside the owner lock. A verified
        receipt whose target this running version reached yields
        ``previous_version`` for the whole sidecar epoch and is marked shown
        (once-only, the same record Qt's completed-update notice consumes), so
        the next start does not report it again. Any read/write failure leaves
        ``previous_version`` null. Returns the previous version or None.
        """
        try:
            with self._persistence_lock:
                notice = app_update_state.completed_update_notice(self.state_path, self.current_version)
                if notice is None or not notice.previous_version:
                    return None
                app_update_state.mark_update_notice_shown(self.state_path, notice.installed_version)
        except Exception as exc:  # Never block startup on the notice record.
            _LOG.warning("Completed-update record unreadable: %s", type(exc).__name__)
            return None
        previous = app_updater.normalize_version(notice.previous_version) or None
        with self._lock:
            self._previous_version = previous
            self._dirty = True
        return previous

    def on_ready(self) -> None:
        with self._lock:
            if self._auto_due is None:
                self._auto_due = self._clock() + AUTO_CHECK_DELAY_SECONDS

    def poll_auto_check(self, submit_normal: Callable[[Callable], bool]) -> bool:
        """Attempt ordinary-domain submission once at ready + 6 seconds.

        No retry on check failure or executor refusal; manual checks stay usable.
        No sleeps, extra threads or mutation-domain work are introduced here.
        """
        with self._lock:
            if self._auto_due is None or self._auto_started or self._clock() < self._auto_due:
                return False
            self._auto_started = True
        return bool(submit_normal(self._automatic_check))

    def _automatic_check(self) -> None:
        try:
            self.check()
        except JobFailure:
            pass  # The topic retains the classified failure; no automatic retry.

    def _offer_notice(self, version: str, *, due: bool) -> None:
        """Commit a worker's notice eligibility result under the owner lock."""
        if (version in self._offered or version in self._deliveries.values()
                or len(self._deliveries) >= 16
                or not due):
            return
        delivery_id = "update-notice-" + uuid4().hex
        self._deliveries[delivery_id] = version
        self._candidates.append({"version": version, "delivery_id": delivery_id})
        self._offered.append(version)

    def take_notice_candidate(self) -> dict[str, str] | None:
        """P2-22 consumes each candidate once and owns its delivery/receipt."""
        with self._lock:
            return dict(self._candidates.popleft()) if self._candidates else None

    def record_notice_delivered(self, version: str, *, delivery_id: str) -> None:
        """P2-22 submits to an ordinary worker after validated shell success."""
        version = app_updater.normalize_version(version)
        with self._persistence_lock:
            with self._lock:
                if self._deliveries.get(delivery_id) != version:
                    raise ValueError("unknown update notice delivery")
            app_update_state.record_notice_delivered(self.state_path, version)
            with self._lock:
                self._deliveries.pop(delivery_id, None)
                self._candidates = deque(c for c in self._candidates if c["delivery_id"] != delivery_id)

    def notice_not_delivered(self, delivery_id: str) -> None:
        """failed/dropped never persist; a new epoch can offer the version again."""
        with self._lock:
            self._deliveries.pop(delivery_id, None)
            self._candidates = deque(c for c in self._candidates if c["delivery_id"] != delivery_id)

    def check(self) -> dict[str, Any]:
        self.poll_handoff()
        with self._lock:
            if self._installing or self._state == "handoff":
                raise JobFailure("conflict")
            flight = self._flight
            leader = flight is None
            if leader:
                flight = self._flight = Future()
                self._transition("checking")
                receipt_ref = self._receipt_ref
        if not leader:
            return deepcopy(flight.result())
        try:
            info = self._updater.check_latest_update(current_version=self.current_version)
            version = app_updater.normalize_version(info.latest_version)
            with self._persistence_lock:
                # The flight excludes installs and installResult(conflict).
                receipt = self.resolve_receipt(receipt_ref) if receipt_ref else None
                due = bool(info.update_available and
                           app_update_state.notice_due(self.state_path, version))
            with self._lock:
                self._last_check_at_ms = self._wall_ms()
                self._info = info
                # Keep a prepared receipt's state only for the same release.
                verified = bool(receipt and info.update_available and
                                receipt["target_version"] == version
                                and self._receipt_ref == receipt_ref)
                if not verified and self._receipt_ref == receipt_ref:
                    self._receipt_ref = None  # Compare-and-clear only what was read.
                # A done check result must carry error=null (PROTOCOL §15), so a
                # kept receipt's earlier launch failure lives on only in
                # last_install_result() and the log, not in this DTO.
                self._transition("verified" if verified else "available" if info.update_available else "idle")
                self._offer_notice(version, due=due)
                result = self._dto()
        except Exception as exc:
            code = "timeout" if isinstance(exc, TimeoutError) else "io" if isinstance(
                exc, (app_updater.AppUpdateError, OSError, ValueError)) else "internal"
            with self._lock:
                self._last_check_at_ms = self._wall_ms()
                self._transition("failed", code)
                failure = JobFailure(code, self._dto())
                flight.set_exception(failure)
            raise failure from exc
        else:
            flight.set_result(result)
            return deepcopy(result)
        finally:
            with self._lock:
                self._flight = None

    def install(self) -> dict[str, Any]:
        self.poll_handoff()
        with self._lock:
            if self._flight is not None or self._installing or self._state == "handoff":
                raise JobFailure("conflict")
            info = self._info
            if info is None or not info.installable:
                raise JobFailure("not_allowed")
            self._installing = True
            handoff = self._publish_handoff is not None
            # P2-27: a receipt kept after a declined/expired handoff or a
            # failed launch is reused once its installer bytes re-hash equal.
            reuse = self._receipt_ref if handoff else None
            self._receipt_ref = None
            if reuse is None:
                self._transition("downloading")
        try:
            receipt = self._revalidated_receipt(reuse, info) if reuse else None
            if receipt is not None:
                receipt_ref = receipt["receipt_ref"]
            else:
                if reuse:
                    with self._lock:
                        self._transition("downloading")
                receipt_ref = self._download_receipt(info)
                if handoff:
                    receipt = self.resolve_receipt(receipt_ref)
                    if receipt is None:
                        raise app_updater.AppUpdateError("verified update receipt unreadable")
            with self._lock:
                self._receipt_ref = receipt_ref
                self._transition("verified")
            if handoff:
                # Still _installing: no check/install can interleave here.
                self._start_handoff(receipt)
            return {"verified": True, "handoff_pending": True, "receipt_ref": receipt_ref}
        except _HandoffUnpublished as exc:
            # The receipt stays valid; state is verified with the io error.
            raise JobFailure("io") from exc.__cause__
        except Exception as exc:
            code = "io" if isinstance(exc, (app_updater.AppUpdateError, OSError, ValueError)) else "internal"
            with self._lock:
                self._transition("failed", code)
            raise JobFailure(code) from exc
        finally:
            with self._lock:
                self._installing = False

    def _download_receipt(self, info: app_updater.AppUpdateInfo) -> str:
        # Re-download can overwrite/delete the previously verified bytes.
        # Revoke the durable receipt first, including across a restart.
        with self._persistence_lock:
            app_update_state.discard_pending_update(self.state_path)
        downloaded = self._updater.download_update(info, self.download_dir)
        if (downloaded.manifest_verified is not True
                or not app_updater.MANIFEST_KEY_ID_RE.fullmatch(downloaded.manifest_key_id or "")
                or not app_updater.SHA256_RE.fullmatch(downloaded.sha256)
                or downloaded.update != info
                or downloaded.installer_path != self.download_dir / info.installer.name):
            raise app_updater.AppUpdateError("invalid verified update receipt")
        receipt_ref = "update-receipt-" + uuid4().hex
        with self._persistence_lock:
            app_update_state.record_pending_update(self.state_path, downloaded, receipt_ref=receipt_ref)
        return receipt_ref

    def _revalidated_receipt(self, receipt_ref: str, info: app_updater.AppUpdateInfo) -> dict[str, Any] | None:
        """Worker-only: durable receipt for this release whose bytes still match."""
        receipt = self.resolve_receipt(receipt_ref)
        if receipt is None or receipt["target_version"] != app_updater.normalize_version(info.latest_version):
            return None
        try:
            digest = app_updater.sha256_file(receipt["installer_path"])
        except OSError:
            return None
        return receipt if digest.lower() == receipt["installer_sha256"].lower() else None

    # ---- P2-27 shell handoff -------------------------------------------------

    def attach_handoff(self, publisher: HandoffPublisher, exit_port: UpdateExitPort) -> None:
        """Enable the handoff. Without it install stops at verified (P2-26)."""
        with self._lock:
            self._publish_handoff = publisher
            self._exit_port = exit_port

    def _start_handoff(self, receipt: dict[str, Any]) -> None:
        handoff_id = "update-handoff-" + uuid4().hex
        data = {
            "handoff_id": handoff_id,
            "receipt_ref": receipt["receipt_ref"],
            "version": receipt["target_version"],
            "verified": True,
            "expires_at_ms": int(self._wall_ms()) + int(self._handoff_seconds * 1000),
            # Shell-only facts (Claude ruling 10-05): the shell re-hashes and
            # launches; Python never starts the installer.
            "installer_path": os.path.abspath(receipt["installer_path"]),
            "sha256": receipt["installer_sha256"].lower(),
            "args": list(INSTALLER_ARGS),
        }
        with self._lock:
            self._handoff = {"id": handoff_id, "receipt_ref": receipt["receipt_ref"],
                             "deadline": self._clock() + self._handoff_seconds,
                             "responded": False, "exit_deadline": None}
            self._transition("handoff")
            publisher = self._publish_handoff
        try:
            publisher.publish_prepare_exit(deepcopy(data))
        except Exception as exc:
            with self._lock:
                if self._handoff is not None and self._handoff["id"] == handoff_id:
                    self._handoff = None
                    self._transition("verified", "io")
            raise _HandoffUnpublished() from exc

    def _advance_handoff_locked(self, started: bool) -> tuple[str, str, str] | None:
        """Apply the two handoff windows; return a log event, never log here."""
        handoff = self._handoff
        if handoff is None or self._exit_observed:
            return None
        now = self._clock()
        if handoff["responded"]:
            if started:
                # The shell's app.quit began this epoch's exit. Quitting is
                # permanent, so the handoff lock stays until the process ends.
                self._exit_observed = True
                return ("exit started", handoff["id"], handoff["receipt_ref"])
            if now < handoff["exit_deadline"]:
                return None
            # Shell accepted but never quit: withdraw the takeover, keep receipt.
            self._handoff = None
            self._transition("verified", "lifecycle_busy")
            return ("accepted without exit", handoff["id"], handoff["receipt_ref"])
        if now < handoff["deadline"]:
            return None
        # Shell gone or silent: drop the exit lock, keep the verified receipt.
        self._handoff = None
        self._transition("verified", "timeout")
        return ("expired", handoff["id"], handoff["receipt_ref"])

    def poll_handoff(self) -> bool:
        """Bridge tick helper; check/install/exitResponse also run it lazily.

        Memory-only apart from one non-blocking exit_started() read made
        outside the owner lock. True if a handoff lock fell back to verified.
        """
        with self._lock:
            handoff = self._handoff
            port = (self._exit_port if handoff is not None and handoff["responded"]
                    and not self._exit_observed else None)
        started = False
        if port is not None:
            try:
                started = bool(port.exit_started())
            except Exception:
                started = False  # Unknown is not an exit; the window still bounds it.
        with self._lock:
            event = self._advance_handoff_locked(started)
        if event is None:
            return False
        _LOG.info("update handoff %s: handoff=%s receipt=%s", *event)
        return event[0] != "exit started"

    def exit_response(self, handoff_id: str, accepted: bool, reason_code: str | None = None) -> dict[str, Any]:
        """update.exitResponse; control domain, records the shell's answer only.

        PROTOCOL §13: accepted=true is a takeover; the shell then sends
        app.quit{reason:update}. Nothing here submits, requests or waits for
        an exit, so the reply is never displaced by the quit (runtime_gone).
        """
        self.poll_handoff()
        with self._lock:
            handoff = self._handoff
            if handoff is None or handoff["id"] != handoff_id or handoff["responded"]:
                raise JobFailure("conflict")
            if accepted:
                handoff["responded"] = True  # No second accept, no confirmation expiry.
                handoff["exit_deadline"] = self._clock() + self._exit_start_seconds
            else:
                self._handoff = None
                self._transition("verified", "cancelled")
        if accepted:
            _LOG.info("update handoff accepted: handoff=%s awaiting shell app.quit", handoff_id)
        else:
            _LOG.info("update handoff declined: handoff=%s reason=%s", handoff_id, _safe_code(reason_code))
        return {"recorded": True}

    def _install_result_busy_locked(self, receipt_ref: str) -> bool:
        """launched=false must not race an install, a check, an unanswered or
        foreign handoff, another in-memory receipt, or an exit already begun."""
        handoff = self._handoff
        return bool(self._installing or self._flight is not None or self._exit_observed
                    or (handoff is not None and (not handoff["responded"]
                                                 or handoff["receipt_ref"] != receipt_ref))
                    or self._receipt_ref not in (None, receipt_ref))

    def install_result(self, receipt_ref: str, launched: bool, reason_code: str | None = None) -> dict[str, Any]:
        """update.installResult; the receipt may come from before a restart."""
        launched = bool(launched)
        reason = _safe_code(reason_code)
        with self._lock:
            if not launched and self._install_result_busy_locked(receipt_ref):
                raise JobFailure("conflict")
            known = receipt_ref == self._receipt_ref
        if not known:
            # Cross-restart: one strict read must resolve the durable receipt.
            # Runs on the control worker and may wait on _persistence_lock
            # behind an ordinary worker's bounded state-file I/O (see report).
            try:
                receipt = self.resolve_receipt(receipt_ref, strict=True)
            except OSError as exc:
                raise JobFailure("io") from exc
            if receipt is None:
                raise JobFailure("conflict")
        with self._lock:
            if not launched and self._install_result_busy_locked(receipt_ref):
                raise JobFailure("conflict")  # A check/install began meanwhile.
            self._last_install_result = {"receipt_ref": receipt_ref, "launched": launched,
                                         "reason_code": reason}
            if not launched:
                self._handoff = None  # Only a responded handoff for this receipt.
                self._receipt_ref = receipt_ref  # Kept for retry; never launched here.
                self._transition("failed", "io")
        if launched:
            _LOG.info("update installer launched: receipt=%s", receipt_ref)
        else:
            _LOG.warning("update installer launch failed: receipt=%s reason=%s", receipt_ref, reason)
        return {"recorded": True}

    def last_install_result(self) -> dict[str, Any] | None:
        with self._lock:
            return deepcopy(self._last_install_result)

    def tick(self, record_state: Callable[[str, dict[str, Any]], Any],
             submit_normal: Callable[[Callable], bool]) -> None:
        """Bridge tick helper: memory-only, never blocks on disk or network."""
        self.poll_handoff()
        self.poll_auto_check(submit_normal)
        template = self.take_state()
        if template is None:
            return
        try:
            record_state(TOPIC, template["data"])
        except Exception:
            with self._lock:
                self._dirty = True  # Retry on the next tick.

    def resolve_receipt(self, receipt_ref: str, *, strict: bool = False) -> dict[str, Any] | None:
        """Local-only P2-27 lookup, also after controlled restart; never a wire DTO.

        Blocking disk lookup: P2-27 must call it from an ordinary worker.
        This is a persisted verification receipt, not launch authorization.
        P2-27 must revalidate installer bytes and perform the exit handshake.
        strict=True raises OSError instead of treating an unreadable file as
        "no receipt" (installResult maps that to io, not conflict).
        """
        with self._persistence_lock:
            pending = app_update_state._read_state(self.state_path, strict=strict).get("pending")
            if not isinstance(pending, dict) or pending.get("receipt_ref") != receipt_ref:
                return None
            version = pending.get("target_version")
            name = pending.get("installer_name")
            if (not isinstance(version, str) or not isinstance(name, str)
                    or name != f"Tesseract-Setup-{version}-win-x64.exe"
                    or Path(name).name != name or pending.get("manifest_verified") is not True
                    or not app_updater.SHA256_RE.fullmatch(str(pending.get("installer_sha256", "")))
                    or not app_updater.MANIFEST_KEY_ID_RE.fullmatch(str(pending.get("manifest_key_id", "")))):
                return None
            return {**deepcopy(pending), "installer_path": self.download_dir / name}

    def check_handler(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        return self.check()

    def install_handler(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        return self.install()

    def exit_response_handler(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        return self.exit_response(args["handoff_id"], args["accepted"], args.get("reason_code"))

    def install_result_handler(self, args: dict[str, Any], context: Any) -> dict[str, Any]:
        return self.install_result(args["receipt_ref"], args["launched"], args.get("reason_code"))

    def register(self, executor: Any) -> None:
        """Existing command specs route both handlers into the normal domain."""
        executor.register("app.update.check", self.check_handler)
        executor.register("app.update.install", self.install_handler)

    def register_shell(self, executor: Any) -> None:
        """Shell-only control-domain replies (PROTOCOL §10 update.*)."""
        executor.register("update.exitResponse", self.exit_response_handler)
        executor.register("update.installResult", self.install_result_handler)


class _HandoffUnpublished(Exception):
    """update.prepareExit could not be recorded; the receipt stays verified."""


def _safe_code(value: Any) -> str | None:
    """Shell reason codes are logged only as short identifier-like codes."""
    if not isinstance(value, str):
        return None
    return value if 0 < len(value) <= 64 and value.replace("_", "").replace(".", "").isalnum() \
        and value.isascii() else "unrecognized"


def register(session: Any, owner: UpdateOwner, *,
             exit_port: UpdateExitPort | None = None,
             publisher: HandoffPublisher | None = None) -> None:
    """Integration entry: ports onto session.state / session.shutdown, then
    the four update commands onto session.executor (bridge_commands specs).

    session.shutdown is read lazily on each tick, so register may run before
    the coordinator exists; until then exit_started() reads as "not started".
    update.state stays a pull: call owner.tick(session.state.record_state,
    session.executor.submit_normal) from the bridge tick, and owner.on_ready()
    once at ready. No queries are registered by this ticket.
    """
    owner.attach_handoff(
        publisher or StateHandoffPublisher(session.state),
        exit_port or ShutdownExitAdapter(lambda: session.shutdown))
    owner.register(session.executor)
    owner.register_shell(session.executor)
    # T-P4-C1 #7: one small state-file read at startup (registration runs on
    # the session's connect path, before the first update.state snapshot).
    owner.load_completed_update()
