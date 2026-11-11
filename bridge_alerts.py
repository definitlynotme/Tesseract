"""``alerts.test``: the settings page's 预览提醒 (P2-23; 提醒 redesign 10-30).

Notices are local only (plan .build/提醒模块重设计_20261030.md): there are no
external channels to test any more. ``alerts.test`` shows one preview notice
through the desktop-notice owner's forced entry (always a card) and waits for
the shell's real delivery receipt. Through ``register`` that wait is a
deferred completion (T-P2-31a): the job stays running without holding a
normal worker until the receipt (or the 15 s cap) settles it.

Only fixed reason codes leave this module.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from bridge_jobs import JobFailure, Pending


# 10-06 ruling (决定_第一波裁定 T-P2-22 #6): the receipt wait is capped at
# 15 s. T-P2-31a: the registered handler waits through deferred completion
# (no worker held); the blocking form remains for direct callers and for
# desktop owners without ``watch_desktop_receipt``. Either way the owner's
# preview slot is released when the wait gives up.
DESKTOP_RECEIPT_TIMEOUT_SECONDS = 15.0


class DesktopTestPort(Protocol):
    """The desktop-notice owner's forced entry (bridge_alerts_desktop).

    ``force_desktop_test`` bypasses the switches, publishes an
    ``alerts.desktop`` item for the shell and returns its ``delivery_id``.
    ``wait_desktop_receipt`` blocks until ``alerts.desktopDelivered`` for that
    id arrives (``{"outcome": delivered|failed|dropped, "surface": ...}``) or
    returns None on timeout.
    """

    def force_desktop_test(self, draft: Any = None) -> str: ...

    def wait_desktop_receipt(self, delivery_id: str,
                             timeout: float) -> Mapping[str, Any] | None: ...

    # Optional deferred-completion form (T-P2-31a, DesktopAlertOwner):
    # watch_desktop_receipt(delivery_id, callback) -> bool and
    # release_desktop_test(delivery_id) -> None.


class UnavailableDesktopTest:
    """Placeholder until the desktop owner is wired; never fakes a receipt."""

    def force_desktop_test(self, draft: Any = None) -> str:
        raise NotImplementedError("desktop notice owner is not wired")

    def wait_desktop_receipt(self, delivery_id: str, timeout: float):
        raise NotImplementedError("desktop receipt wait is not wired")


def _error(code: str, reason: str) -> dict[str, Any]:
    # Generic per-code key (same family as the executor's own errors.<code>);
    # the fixed reason_code carries the detail.
    return {"code": code, "message_key": f"errors.{code}", "params": {},
            "reason_code": reason}


def _failed(code: str, reason: str, delivery_id: str | None = None) -> dict[str, Any]:
    return {"ok": False, "error": _error(code, reason), "delivery_id": delivery_id}


def _desktop_failure(exc: JobFailure, delivery_id: str | None = None) -> dict[str, Any]:
    """The owner raises typed JobFailure: keep its meaning instead of io."""
    if exc.code == "conflict":      # another preview is in flight
        return _failed("conflict", "alerts_desktop_busy", delivery_id)
    return _failed("io", "alerts_desktop_failed", delivery_id)


class AlertTester:
    """``alerts.test`` job (normal domain, cancellable only while queued)."""

    def __init__(self, *, desktop: DesktopTestPort | None = None,
                 receipt_timeout: float = DESKTOP_RECEIPT_TIMEOUT_SECONDS,
                 deferred: bool = False) -> None:
        self.desktop = desktop or UnavailableDesktopTest()
        # Never above the ruled cap, whatever a caller passes.
        self.receipt_timeout = min(float(receipt_timeout), DESKTOP_RECEIPT_TIMEOUT_SECONDS)
        # True (register): return a Pending for the desktop receipt wait when
        # the desktop owner offers watch_desktop_receipt.
        self.deferred = deferred

    def execute(self, args: dict[str, Any], context: Any) -> Any:
        try:
            delivery_id = self.desktop.force_desktop_test()
        except NotImplementedError:
            return _failed("runtime_unavailable", "alerts_desktop_unavailable")
        except JobFailure as exc:
            return _desktop_failure(exc)
        except Exception:
            return _failed("io", "alerts_desktop_failed")
        if not isinstance(delivery_id, str) or not delivery_id:
            return _failed("io", "alerts_desktop_failed")
        watch = getattr(self.desktop, "watch_desktop_receipt", None)
        release = getattr(self.desktop, "release_desktop_test", None)
        if self.deferred and callable(watch) and callable(release):
            return self._desktop_deferred(delivery_id, watch, release)
        try:
            # On timeout the owner releases its preview slot itself.
            receipt = self.desktop.wait_desktop_receipt(delivery_id, self.receipt_timeout)
        except NotImplementedError:
            return _failed("runtime_unavailable", "alerts_desktop_unavailable", delivery_id)
        except JobFailure as exc:
            return _desktop_failure(exc, delivery_id)
        except Exception:
            return _failed("io", "alerts_desktop_failed", delivery_id)
        return self._receipt_result(receipt, delivery_id)

    def _desktop_deferred(self, delivery_id: str, watch: Any, release: Any) -> Any:
        """T-P2-31a: the job waits for the shell's receipt without a worker.

        The receipt (owner watcher) settles the Pending; the 15 s cap ends it
        as ``timeout`` and every abandonment (timeout, new epoch) frees the
        owner's preview slot, exactly like a blocking wait that gave up.
        """
        pending = Pending(
            timeout=self.receipt_timeout,
            on_timeout=lambda: _failed("timeout", "alerts_desktop_receipt_timeout", delivery_id),
            on_abandon=lambda _reason: release(delivery_id))
        try:
            known = watch(delivery_id, lambda receipt: pending.complete(
                self._receipt_result(receipt, delivery_id)))
        except Exception:
            release(delivery_id)
            return _failed("io", "alerts_desktop_failed", delivery_id)
        if not known:
            release(delivery_id)
            return _failed("io", "alerts_desktop_failed", delivery_id)
        return pending

    @staticmethod
    def _receipt_result(receipt: Any, delivery_id: str) -> dict[str, Any]:
        if receipt is None:
            return _failed("timeout", "alerts_desktop_receipt_timeout", delivery_id)
        outcome = receipt.get("outcome") if isinstance(receipt, Mapping) else None
        if outcome == "delivered":
            return {"ok": True, "error": None, "delivery_id": delivery_id}
        if outcome == "dropped":
            return _failed("io", "alerts_desktop_dropped", delivery_id)
        return _failed("io", "alerts_desktop_failed", delivery_id)


def register(session: Any, *, desktop: DesktopTestPort | None = None) -> dict[str, Any]:
    """Register ``alerts.test`` (job, queued cancel) on one bridge session.

    ``desktop`` is the desktop-notice owner exposing the DesktopTestPort
    methods; its receipt wait is a deferred completion (T-P2-31a).
    """
    tester = AlertTester(desktop=desktop, deferred=True)
    session.executor.register("alerts.test", tester.execute)
    return {"tester": tester}
