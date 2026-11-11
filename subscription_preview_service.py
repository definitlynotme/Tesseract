from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable

from collector import collect_nodes_from_sources_with_report
from config_utils import enabled_subscription_urls
from exit_nodes import mark_exit_node
from models import Node, count_healthy_nodes, utc_now_iso
from runtime_settings import (
    client_config_block,
    client_dns_block,
    egress_proxy_block,
    testing_block,
    tier_block,
)
from subscription_operations import (
    SubscriptionOperationCoordinator,
    SubscriptionOperationLease,
)
from tester import test_nodes
from tier3_collection_settings import collection_settings


ConfigProvider = Callable[[], dict[str, Any]]
AsyncRunner = Callable[[Awaitable[Any]], Any]
CollectionSettingsProvider = Callable[[dict[str, Any]], dict[str, Any]]


class SubscriptionPreviewService:
    """Download and test one subscription without publishing runtime state.

    The service owns only request-local cancellation events and returns status
    snapshots suitable for temporary UI presentation.  It never receives a
    Manager or Core reference and therefore cannot update pools, check clocks,
    observation caches, persistent snapshots, or the active Mihomo config.
    """

    def __init__(
        self,
        config_provider: ConfigProvider | dict[str, Any],
        logger: logging.Logger,
        *,
        operation_coordinator: SubscriptionOperationCoordinator | None = None,
        collector: Callable[..., Awaitable[tuple[list[Node], Any]]] = (
            collect_nodes_from_sources_with_report
        ),
        tester: Callable[..., Awaitable[list[Node]]] = test_nodes,
        async_runner: AsyncRunner | None = None,
        collection_settings_provider: CollectionSettingsProvider | None = None,
    ) -> None:
        if callable(config_provider):
            self._config_provider = config_provider
        else:
            self._config_provider = lambda: config_provider
        self.logger = logger
        self.operation_coordinator = (
            operation_coordinator or SubscriptionOperationCoordinator()
        )
        self._collector = collector
        self._tester = tester
        self._async_runner = async_runner
        self._collection_settings_provider = collection_settings_provider
        self._request_lock = threading.RLock()
        self._request_cancel_events: dict[str, threading.Event] = {}

    @staticmethod
    def _subscription_identity(item: dict[str, Any]) -> tuple[str, str]:
        tier = str(item.get("tier") or "").strip().lower()
        kind = str(item.get("kind") or "").strip().lower()
        url = str(
            item.get("url") or item.get("uri") or item.get("key") or ""
        ).strip()
        if kind != "subscription" or not url:
            raise ValueError("subscription item is required")
        if tier not in {"tier2", "tier3", "exit"}:
            raise ValueError(
                "manual subscription diagnosis is supported for tier2, tier3, and exit"
            )
        return tier, url

    @staticmethod
    def _config_identity(tier: str, url: str) -> str:
        return hashlib.sha256(f"{tier}\0{url}".encode("utf-8")).hexdigest()

    @staticmethod
    def _subscription_is_enabled(
        config: dict[str, Any],
        tier: str,
        url: str,
    ) -> bool:
        return url in enabled_subscription_urls(
            tier_block(config, tier).get("subscription_urls", [])
        )

    @staticmethod
    def _collection_settings(config: dict[str, Any]) -> dict[str, Any]:
        settings = collection_settings(
            dict(testing_block(config)),
            egress_proxy_block(config),
        )
        # This service has no authoritative Core/effective-egress reference.
        # Remote previews therefore require an explicit egress proxy; local
        # files remain available through the collector's route gate.
        settings["require_remote_source_route"] = True
        return settings

    def _effective_collection_settings(
        self,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        provider = self._collection_settings_provider
        if callable(provider):
            settings = provider(config)
            if not isinstance(settings, dict):
                raise RuntimeError("subscription preview route provider returned invalid settings")
            return dict(settings)
        return self._collection_settings(config)

    @staticmethod
    def _testing_config(
        config: dict[str, Any],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        testing = dict(testing_block(config))
        testing["validation_cancel_event"] = cancel_event
        client = dict(client_config_block(config))
        binary_path = client.get("mihomo_binary_path")
        if binary_path and not testing.get("mihomo_binary_path"):
            testing["mihomo_binary_path"] = str(binary_path)
        work_dir = client.get("mihomo_work_dir")
        if work_dir and not testing.get("mihomo_validation_temp_dir"):
            testing["mihomo_validation_temp_dir"] = str(
                Path(str(work_dir)) / "validation"
            )
        dns = client_dns_block(client)
        if "ipv6" not in testing:
            testing["ipv6"] = bool(client.get("ipv6", dns.get("ipv6", False)))
        return testing

    @staticmethod
    def _full_validation_enabled(config: dict[str, Any]) -> bool:
        mode = str(testing_block(config).get("validation_mode", "optional")).lower()
        return mode in {"optional", "full"}

    @staticmethod
    def _collection_report_payload(report: Any) -> dict[str, Any]:
        serializer = getattr(report, "to_dict", None)
        payload = serializer() if callable(serializer) else {}
        if not isinstance(payload, dict):
            return {}
        # Source reports include the configured URL and may therefore contain
        # an access token.  A temporary UI diagnostic only needs aggregates.
        return {
            key: payload.get(key)
            for key in (
                "source_count",
                "configured_source_count",
                "fetched_sources",
                "failed_sources",
                "node_count",
                "discovered_source_units",
                "fetched_source_units",
                "failed_source_units",
                "pending_source_units",
                "retry_due_source_units",
            )
            if key in payload
        }

    @staticmethod
    def _finish_lease(
        coordinator: SubscriptionOperationCoordinator,
        lease: SubscriptionOperationLease | Any,
    ) -> None:
        lease_finish = getattr(lease, "finish", None)
        if callable(lease_finish):
            lease_finish()
            return
        coordinator.finish(lease)

    def _register_request(
        self,
        request_id: str,
        cancel_event: threading.Event,
    ) -> None:
        with self._request_lock:
            previous = self._request_cancel_events.get(request_id)
            self._request_cancel_events[request_id] = cancel_event
        if previous is not None and previous is not cancel_event:
            previous.set()

    def _release_request(
        self,
        request_id: str,
        cancel_event: threading.Event,
    ) -> None:
        with self._request_lock:
            if self._request_cancel_events.get(request_id) is cancel_event:
                self._request_cancel_events.pop(request_id, None)

    def cancel(self, request_id: str) -> bool:
        request_key = str(request_id or "").strip()
        if not request_key:
            return False
        with self._request_lock:
            cancel_event = self._request_cancel_events.get(request_key)
        if cancel_event is None:
            return False
        cancel_event.set()
        return True

    def cancel_all(self) -> int:
        with self._request_lock:
            cancel_events = tuple(self._request_cancel_events.values())
        for cancel_event in cancel_events:
            cancel_event.set()
        return len(cancel_events)

    def active_request_ids(self) -> tuple[str, ...]:
        with self._request_lock:
            return tuple(self._request_cancel_events)

    @staticmethod
    def _base_result(
        *,
        request_id: str,
        tier: str,
        url: str,
        config_identity: str,
        started_at: str,
    ) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "temporary": True,
            "diagnostic_only": True,
            "observation_origin": "manual_preview",
            "tier": tier,
            "url": url,
            "subscription_id": config_identity,
            "config_identity": config_identity,
            "started_at": started_at,
            "observed_at": None,
            "completed_at": None,
            "status": "running",
            "reason": "",
            "cancelled": False,
            "blocked_by_system": False,
            "total": 0,
            "healthy": 0,
            "nodes": [],
            "collection": {},
            "error": "",
        }

    @staticmethod
    def _cancelled_result(result: dict[str, Any]) -> dict[str, Any]:
        completed_at = utc_now_iso()
        result.update(
            {
                "status": "cancelled",
                "reason": "cancelled",
                "cancelled": True,
                "observed_at": completed_at,
                "completed_at": completed_at,
                "total": 0,
                "healthy": 0,
                "nodes": [],
                "error": "",
            }
        )
        return result

    def _run_awaitable(self, awaitable: Awaitable[Any]) -> Any:
        if self._async_runner is not None:
            return self._async_runner(awaitable)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or not loop.is_running():
            return asyncio.run(awaitable)

        result: dict[str, Any] = {}
        completed = threading.Event()

        def runner() -> None:
            try:
                result["value"] = asyncio.run(awaitable)
            except BaseException as exc:  # propagate into the caller thread
                result["error"] = exc
            finally:
                completed.set()

        worker = threading.Thread(
            target=runner,
            name="subscription-preview-async",
            daemon=True,
        )
        worker.start()
        completed.wait()
        error = result.get("error")
        if isinstance(error, BaseException):
            raise error
        return result.get("value")

    async def _diagnose_async(
        self,
        *,
        tier: str,
        url: str,
        cancel_event: threading.Event,
        config: dict[str, Any],
    ) -> tuple[list[Node], Any]:
        origin = "tier3a" if tier == "tier3" else tier
        nodes, report = await self._collector(
            source_entries=[
                {
                    "type": "subscription_url",
                    "value": url,
                    "enabled": True,
                    "origin": origin,
                }
            ],
            tier=tier,
            logger=self.logger,
            settings=self._effective_collection_settings(config),
            cancel_event=cancel_event,
        )
        if cancel_event.is_set() or not nodes:
            return [], report
        prepared: list[Node] = []
        for index, source_node in enumerate(nodes):
            node = source_node.clone()
            node.tier = tier
            if not isinstance(node.extra, dict):
                node.extra = {}
            if tier == "tier3":
                node.extra["tier3_origin"] = "tier3a"
                node.extra["source_origin"] = "tier3a"
            elif tier == "exit":
                mark_exit_node(node, index=index)
            prepared.append(node)
        tested = await self._tester(
            prepared,
            testing_config=self._testing_config(config, cancel_event),
            logger=self.logger,
            full_validation_enabled=self._full_validation_enabled(config),
            gateway_nodes=[],
            cancel_event=cancel_event,
        )
        return ([] if cancel_event.is_set() else list(tested)), report

    def diagnose_subscription(
        self,
        item: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        tier, url = self._subscription_identity(item)
        request_key = str(request_id or "").strip()
        if not request_key:
            raise ValueError("subscription diagnosis request_id is required")
        started_at = utc_now_iso()
        config_identity = self._config_identity(tier, url)
        result = self._base_result(
            request_id=request_key,
            tier=tier,
            url=url,
            config_identity=config_identity,
            started_at=started_at,
        )
        cancel_event = threading.Event()
        self._register_request(request_key, cancel_event)
        lease: SubscriptionOperationLease | Any | None = None
        try:
            system_was_active = self.operation_coordinator.is_system_active(
                tier,
                url,
            )
            lease = self.operation_coordinator.try_begin_manual(
                tier,
                url,
                cancel_event=cancel_event,
            )
            if lease is None:
                completed_at = utc_now_iso()
                automatic_refresh_active = bool(
                    system_was_active
                    or self.operation_coordinator.is_system_active(tier, url)
                )
                result.update(
                    {
                        "status": "blocked",
                        "reason": (
                            "automatic_refresh_in_progress"
                            if automatic_refresh_active
                            else "manual_diagnosis_in_progress"
                        ),
                        "blocked_by_system": automatic_refresh_active,
                        "observed_at": completed_at,
                        "completed_at": completed_at,
                        "error": (
                            "automatic subscription refresh is in progress"
                            if automatic_refresh_active
                            else "subscription diagnosis is already in progress"
                        ),
                    }
                )
                return result
            config = copy.deepcopy(dict(self._config_provider() or {}))
            if not self._subscription_is_enabled(config, tier, url):
                completed_at = utc_now_iso()
                result.update(
                    {
                        "status": "stale",
                        "reason": "subscription_config_changed",
                        "observed_at": completed_at,
                        "completed_at": completed_at,
                        "error": "subscription is no longer enabled",
                    }
                )
                return result
            try:
                nodes, report = self._run_awaitable(
                    self._diagnose_async(
                        tier=tier,
                        url=url,
                        cancel_event=cancel_event,
                        config=config,
                    )
                )
            except asyncio.CancelledError:
                cancel_event.set()
                return self._cancelled_result(result)
            except Exception as exc:
                if cancel_event.is_set():
                    return self._cancelled_result(result)
                self.logger.warning(
                    "Temporary subscription diagnosis failed for %s",
                    tier,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                completed_at = utc_now_iso()
                result.update(
                    {
                        "status": "failed",
                        "reason": "diagnosis_failed",
                        "observed_at": completed_at,
                        "completed_at": completed_at,
                        "error": "subscription diagnosis failed",
                    }
                )
                return result
            result["collection"] = self._collection_report_payload(report)
            if cancel_event.is_set():
                return self._cancelled_result(result)
            current_config = copy.deepcopy(dict(self._config_provider() or {}))
            if not self._subscription_is_enabled(current_config, tier, url):
                completed_at = utc_now_iso()
                result.update(
                    {
                        "status": "stale",
                        "reason": "subscription_config_changed",
                        "observed_at": completed_at,
                        "completed_at": completed_at,
                        "total": 0,
                        "healthy": 0,
                        "nodes": [],
                        "error": "subscription changed while diagnosis was running",
                    }
                )
                return result
            observed_at = utc_now_iso()
            if not nodes:
                result.update(
                    {
                        "status": "empty",
                        "reason": "no_nodes",
                        "observed_at": observed_at,
                        "completed_at": observed_at,
                        "error": "subscription diagnosis returned no nodes",
                    }
                )
                return result
            node_payloads: list[dict[str, Any]] = []
            for index, node in enumerate(nodes):
                private_identity = "|".join(
                    [
                        request_key,
                        str(index),
                        node.protocol,
                        node.address,
                        str(node.port),
                    ]
                )
                payload = {
                    "id": hashlib.sha256(private_identity.encode("utf-8")).hexdigest()[:12],
                    "name": node.name,
                    "protocol": node.protocol,
                    "address": node.address,
                    "port": node.port,
                    "tier": node.tier,
                    "status": (
                        node.status.value
                        if hasattr(node.status, "value")
                        else str(node.status or "")
                    ),
                    "latency_ms": node.latency_ms,
                    "last_checked_at": node.last_checked_at,
                    "suspicious_reason": node.suspicious_reason,
                    "observation_origin": "manual_preview",
                    "observed_at": node.last_checked_at or observed_at,
                    "request_id": request_key,
                    "diagnostic_only": True,
                }
                node_payloads.append(payload)
            result.update(
                {
                    "status": "completed",
                    "reason": "manual_diagnosis_completed",
                    "observed_at": observed_at,
                    "completed_at": observed_at,
                    "total": len(nodes),
                    "healthy": count_healthy_nodes(nodes),
                    "nodes": node_payloads,
                }
            )
            return result
        finally:
            if lease is not None:
                self._finish_lease(self.operation_coordinator, lease)
            self._release_request(request_key, cancel_event)


__all__ = ["SubscriptionPreviewService"]
