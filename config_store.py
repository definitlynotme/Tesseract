from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml

from atomic_file import replace_with_retry
from config_item_identity import imported_node_uri, source_entry_value
from config_items_view import configured_config_items
from config_summary_view import (
    config_summary,
    config_summary_with_runtime_status,
    summarize_url as config_summarize_url,
    sources_summary as config_sources_summary,
)
from config_utils import (
    coerce_bool,
    config_entry_enabled,
    ensure_list_section,
    ensure_mapping_section,
    subscription_entry_url,
)
from config_validation import (
    validate_local_config,
    validate_sources_config,
    validate_tier1_node,
    validate_url,
)
from exit_nodes import exit_nodes_from_entry
from gateway_selection import (
    clear_gateway_selection,
    gateway_identity,
    read_selection,
    write_bound_selection,
    write_name_only_selection,
    write_unresolved_selection,
)
from node_status_view import protocols_update_payload
from parser import parse_node_uri
from windows_file_security import restrict_to_current_user


SOURCE_SECTIONS = {"github_repos", "telegram_channels", "subscription_urls", "local_files"}


def _imported_node_selected_names(entry: Any) -> set[str]:
    names: set[str] = set()
    configured_name = ""
    if isinstance(entry, dict):
        configured_name = str(entry.get("name") or entry.get("label") or "").strip()
        if configured_name:
            names.add(configured_name)
    uri = imported_node_uri(entry).strip()
    fragment = ""
    if "#" in uri:
        fragment = uri.rsplit("#", 1)[1].strip()
        if fragment:
            names.add(unquote(fragment))
    if uri and not configured_name and not fragment:
        parsed_node = parse_node_uri(uri)
        if parsed_node is not None:
            names.update(
                str(value or "").strip()
                for value in (parsed_node.name, parsed_node.address)
                if str(value or "").strip()
            )
    return names


# Parses configured exit entries for their identities only; the runtime's own
# load reports any entry it cannot use.
_QUIET_LOGGER = logging.Logger("config-store-exit-identity")
_QUIET_LOGGER.addHandler(logging.NullHandler())


def _exit_entry_identities(entry: Any, index: int = 0) -> set[str]:
    try:
        nodes = exit_nodes_from_entry(entry, index, logger=_QUIET_LOGGER)
    except Exception:
        return set()
    return {gateway_identity(node) for node in nodes}


def _clear_exit_selection_for_nodes(
    exit_payload: dict[str, Any],
    nodes: list[Any],
    *,
    keys: tuple[str, ...] = (),
    remaining: list[Any] | tuple[Any, ...] = (),
) -> bool:
    """Turn the Gateway off when its own node is removed or disabled.

    A selection bound to an identity is cleared only when a removed entry
    carries that identity and no remaining enabled entry does (deleting a
    same-named twin keeps it).  Name-only selections keep the name and URI
    rule.  Every clear removes the label and the record.
    """

    selection = read_selection(exit_payload)
    if not selection.active:
        return False
    if selection.state == "bound":
        removed = set().union(*(_exit_entry_identities(node, index) for index, node in enumerate(nodes)))
        if selection.identity not in removed:
            return False
        if any(
            isinstance(entry, (dict, str))
            and config_entry_enabled(entry)
            and selection.identity in _exit_entry_identities(entry, index)
            for index, entry in enumerate(remaining)
        ):
            return False
        return clear_gateway_selection(exit_payload)

    selected_names = {
        str(exit_payload.get(field) or "").strip()
        for field in ("selected", "selected_proxy")
    }
    selected_names.discard("")
    if not selected_names:
        return False

    node_names = {str(key).strip() for key in keys if str(key).strip()}
    for node in nodes:
        node_names.update(_imported_node_selected_names(node))
        uri = imported_node_uri(node).strip()
        if uri:
            node_names.add(uri)
    if not selected_names.intersection(node_names):
        return False

    return clear_gateway_selection(exit_payload)


class ConfigStore:
    def __init__(
        self,
        *,
        local_config_path: str | Path = "config.local.yaml",
        sources_path: str | Path = "sources.yaml",
        backup_dir: str | Path = "data/config-backups",
        max_backups_per_file: int = 50,
    ) -> None:
        self.local_config_path = Path(local_config_path)
        self.sources_path = Path(sources_path)
        self.backup_dir = Path(backup_dir)
        self.max_backups_per_file = max(1, int(max_backups_per_file))
        self.lock = threading.RLock()

    def read_local(self) -> dict[str, Any]:
        with self.lock:
            return self._read_yaml(self.local_config_path)

    def write_local(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            validate_local_config(payload)
            backup = self._backup_file(self.local_config_path)
            self._write_yaml(self.local_config_path, payload)
            return {"path": str(self.local_config_path), "backup": str(backup) if backup else None}

    def read_sources(self) -> dict[str, Any]:
        with self.lock:
            return self._read_yaml(self.sources_path)

    def write_sources(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            validate_sources_config(payload)
            backup = self._backup_file(self.sources_path)
            self._write_yaml(self.sources_path, payload)
            return {"path": str(self.sources_path), "backup": str(backup) if backup else None}

    def summary(self) -> dict[str, Any]:
        local_config = self.read_local()
        sources = self.read_sources()
        return config_summary(local_config, sources)

    def configured_items(self) -> list[dict[str, Any]]:
        local_config = self.read_local()
        sources = self.read_sources()
        return configured_config_items(local_config, sources)

    def summary_with_status(self, runtime_status: dict[str, Any] | None) -> dict[str, Any]:
        return config_summary_with_runtime_status(self.summary(), runtime_status)

    def sources_summary(self, sources: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = sources if sources is not None else self.read_sources()
        return config_sources_summary(payload)

    def _summarize_url(self, url: str) -> dict[str, str]:
        return config_summarize_url(url)

    def _tier_payload(self, payload: dict[str, Any], tier: str) -> dict[str, Any]:
        tiers = ensure_mapping_section(payload, "tiers")
        return ensure_mapping_section(tiers, tier)

    def _tier_nodes_payload(self, payload: dict[str, Any], tier: str) -> list[Any]:
        return ensure_list_section(self._tier_payload(payload, tier), "nodes")

    def _tier_subscription_urls_payload(self, payload: dict[str, Any], tier: str) -> list[Any]:
        return ensure_list_section(self._tier_payload(payload, tier), "subscription_urls")

    def add_tier1_node(self, node: dict[str, Any]) -> dict[str, Any]:
        validate_tier1_node(node)
        payload = self.read_local()
        nodes = self._tier_nodes_payload(payload, "tier1")
        name = str(node["name"])
        if any(isinstance(item, dict) and item.get("name") == name for item in nodes):
            raise ValueError(f"tier1 node already exists: {name}")
        nodes.append(dict(node))
        return self.write_local(payload)

    def update_tier1_node(self, name: str, updates: dict[str, Any]) -> dict[str, Any]:
        payload = self.read_local()
        nodes = self._tier_nodes_payload(payload, "tier1")
        for index, node in enumerate(nodes):
            if isinstance(node, dict) and node.get("name") == name:
                merged = {**node, **updates}
                validate_tier1_node(merged)
                nodes[index] = merged
                return self.write_local(payload)
        raise KeyError(f"tier1 node not found: {name}")

    def delete_tier1_node(self, name: str) -> dict[str, Any]:
        payload = self.read_local()
        nodes = self._tier_nodes_payload(payload, "tier1")
        remaining = [node for node in nodes if not (isinstance(node, dict) and node.get("name") == name)]
        if len(remaining) == len(nodes):
            raise KeyError(f"tier1 node not found: {name}")
        payload["tiers"]["tier1"]["nodes"] = remaining
        return self.write_local(payload)

    def add_tier2_subscription(self, url: str, *, enabled: Any = True) -> dict[str, Any]:
        validate_url(url)
        payload = self.read_local()
        urls = self._tier_subscription_urls_payload(payload, "tier2")
        if any(subscription_entry_url(entry) == url for entry in urls):
            raise ValueError("tier2 subscription already exists")
        normalized_enabled = coerce_bool(enabled, True)
        urls.append(url if normalized_enabled else {"url": url, "enabled": False})
        return self.write_local(payload)

    def add_tier2_node(self, node: dict[str, Any]) -> dict[str, Any]:
        uri = str(node.get("uri") or node.get("link") or node.get("url") or "").strip()
        if not uri:
            raise ValueError("tier2 node uri is required")
        payload = self.read_local()
        nodes = self._tier_nodes_payload(payload, "tier2")
        existing = {imported_node_uri(item) for item in nodes}
        if uri in existing:
            raise ValueError("tier2 node already exists")
        nodes.append(dict(node))
        return self.write_local(payload)

    def add_tier3_subscription(self, url: str, *, enabled: Any = True) -> dict[str, Any]:
        validate_url(url)
        payload = self.read_local()
        urls = self._tier_subscription_urls_payload(payload, "tier3")
        if any(subscription_entry_url(entry) == url for entry in urls):
            raise ValueError("tier3 subscription already exists")
        normalized_enabled = coerce_bool(enabled, True)
        urls.append(url if normalized_enabled else {"url": url, "enabled": False})
        return self.write_local(payload)

    def add_tier3_node(self, node: dict[str, Any]) -> dict[str, Any]:
        uri = str(node.get("uri") or node.get("link") or node.get("url") or "").strip()
        if not uri:
            raise ValueError("tier3 node uri is required")
        payload = self.read_local()
        nodes = self._tier_nodes_payload(payload, "tier3")
        existing = {imported_node_uri(item) for item in nodes}
        if uri in existing:
            raise ValueError("tier3 node already exists")
        nodes.append(dict(node))
        return self.write_local(payload)

    def add_exit_subscription(self, url: str, *, enabled: Any = True) -> dict[str, Any]:
        validate_url(url)
        payload = self.read_local()
        urls = self._tier_subscription_urls_payload(payload, "exit")
        if any(subscription_entry_url(entry) == url for entry in urls):
            raise ValueError("exit subscription already exists")
        normalized_enabled = coerce_bool(enabled, True)
        urls.append(url if normalized_enabled else {"url": url, "enabled": False})
        return self.write_local(payload)

    def set_exit_selected_proxy(self, proxy_name: str) -> dict[str, Any]:
        """A name-only Gateway selection ('' turns the Gateway off)."""

        return self.set_exit_selection(str(proxy_name or "").strip())

    def set_exit_selection(
        self,
        label: str,
        *,
        identity: str | None = None,
        unresolved: str | None = None,
    ) -> dict[str, Any]:
        """Save the Gateway selection: the node's identity with its proxy
        name as label (bound), the name alone, or a name proven ambiguous.
        An empty label turns the Gateway off."""

        with self.lock:
            payload = self.read_local()
            self._apply_exit_selection(
                self._tier_payload(payload, "exit"),
                str(label or "").strip(),
                identity=identity,
                unresolved=unresolved,
            )
            return self.write_local(payload)

    def compare_and_set_exit_selection(
        self,
        expected_key: str,
        label: str,
        *,
        identity: str | None = None,
        unresolved: str | None = None,
    ) -> bool:
        """``set_exit_selection`` only while the saved selection's key is
        still ``expected_key`` (a pick made meanwhile wins)."""

        with self.lock:
            payload = self.read_local()
            exit_payload = self._tier_payload(payload, "exit")
            if read_selection(exit_payload).key != expected_key:
                return False
            self._apply_exit_selection(
                exit_payload,
                str(label or "").strip(),
                identity=identity,
                unresolved=unresolved,
            )
            self.write_local(payload)
            return True

    @staticmethod
    def _apply_exit_selection(
        exit_payload: dict[str, Any],
        label: str,
        *,
        identity: str | None,
        unresolved: str | None,
    ) -> None:
        if not label:
            clear_gateway_selection(exit_payload)
        elif identity:
            write_bound_selection(exit_payload, label, identity)
        elif unresolved:
            write_unresolved_selection(exit_payload, label, unresolved)
        else:
            write_name_only_selection(exit_payload, label)

    def delete_tier2_subscription(self, url: str) -> dict[str, Any]:
        return self._delete_tier_subscription("tier2", url)

    def delete_tier2_node(self, key: str) -> dict[str, Any]:
        return self._delete_imported_node("tier2", key)

    def delete_tier3_subscription(self, url: str) -> dict[str, Any]:
        return self._delete_tier_subscription("tier3", url)

    def delete_tier3_node(self, key: str) -> dict[str, Any]:
        return self._delete_imported_node("tier3", key)

    def delete_exit_node(self, key: str) -> dict[str, Any]:
        return self._delete_imported_node("exit", key)

    def delete_exit_subscription(self, url: str) -> dict[str, Any]:
        return self._delete_tier_subscription("exit", url)

    def _delete_tier_subscription(self, tier: str, url: str) -> dict[str, Any]:
        payload = self.read_local()
        urls = self._tier_subscription_urls_payload(payload, tier)
        remaining = [entry for entry in urls if subscription_entry_url(entry) != url]
        if len(remaining) == len(urls):
            raise KeyError(f"{tier} subscription not found")
        payload["tiers"][tier]["subscription_urls"] = remaining
        return self.write_local(payload)

    def delete_source_item(self, section: str, key: str) -> dict[str, Any]:
        if section not in SOURCE_SECTIONS:
            raise ValueError("source section must be github_repos, telegram_channels, subscription_urls, or local_files")
        payload = self.read_sources()
        entries = ensure_list_section(payload, section)
        remaining = [entry for entry in entries if source_entry_value(entry) != key]
        if len(remaining) == len(entries):
            raise KeyError("source item not found")
        payload[section] = remaining
        return self.write_sources(payload)

    def delete_config_item(self, item: dict[str, Any]) -> dict[str, Any]:
        tier = str(item.get("tier", "") or "")
        kind = str(item.get("kind", "") or "")
        key = str(item.get("key", "") or "")
        if not key:
            raise ValueError("config item key is required")
        if tier == "tier1" and kind == "node":
            return self.delete_tier1_node(key)
        if tier == "tier2" and kind == "node":
            return self.delete_tier2_node(key)
        if tier == "tier2" and kind == "subscription":
            return self.delete_tier2_subscription(key)
        if tier == "tier3" and kind == "node":
            return self.delete_tier3_node(key)
        if tier == "tier3" and kind == "subscription":
            return self.delete_tier3_subscription(key)
        if tier == "exit" and kind == "node":
            return self.delete_exit_node(key)
        if tier == "exit" and kind == "subscription":
            return self.delete_exit_subscription(key)
        if tier == "tier3" and kind == "source":
            return self.delete_source_item(str(item.get("section", "") or ""), key)
        raise ValueError(f"unsupported config item: {tier}/{kind}")

    def update_config_item(self, item: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            tier = str(item.get("tier", "") or "")
            kind = str(item.get("kind", "") or "")
            key = str(item.get("key", "") or "")
            if not key:
                raise ValueError("config item key is required")
            if kind == "node":
                if tier == "tier1":
                    return self.update_tier1_node(key, self._tier1_update_payload(updates))
                if tier in {"tier2", "tier3", "exit"}:
                    return self._update_imported_node(tier, key, updates)
            if kind == "subscription" and tier in {"tier2", "tier3", "exit"}:
                return self._update_tier_subscription(tier, key, updates)
            raise ValueError(f"unsupported config item: {tier}/{kind}")

    def set_config_item_enabled(self, item: dict[str, Any], enabled: Any) -> dict[str, Any]:
        return self.update_config_item(item, {"enabled": coerce_bool(enabled, True)})

    def _delete_imported_node(self, tier: str, key: str) -> dict[str, Any]:
        payload = self.read_local()
        nodes = self._tier_nodes_payload(payload, tier)
        removed_nodes = [node for node in nodes if imported_node_uri(node) == key]
        remaining = [node for node in nodes if imported_node_uri(node) != key]
        if len(remaining) == len(nodes):
            raise KeyError(f"{tier} node not found")
        payload["tiers"][tier]["nodes"] = remaining
        if tier == "exit":
            exit_payload = self._tier_payload(payload, "exit")
            _clear_exit_selection_for_nodes(
                exit_payload,
                removed_nodes,
                keys=(key,),
                remaining=remaining,
            )
        return self.write_local(payload)

    def _update_imported_node(self, tier: str, key: str, updates: dict[str, Any]) -> dict[str, Any]:
        payload = self.read_local()
        nodes = self._tier_nodes_payload(payload, tier)
        uri_update_requested = any(field in updates for field in ("uri", "link", "url"))
        requested_uri = str(
            updates.get("uri", "") or updates.get("link", "") or updates.get("url", "") or ""
        ).strip()
        if uri_update_requested and not requested_uri:
            raise ValueError(f"{tier} node uri is required")
        name_update_requested = "name" in updates
        requested_name = str(updates.get("name", "") or "").strip()
        enabled_update_requested = "enabled" in updates
        requested_enabled = coerce_bool(updates.get("enabled"), True)
        for index, node in enumerate(nodes):
            if imported_node_uri(node) != key:
                continue
            original_node = node
            uri = requested_uri if uri_update_requested else imported_node_uri(node).strip()
            if not uri:
                raise ValueError(f"{tier} node uri is required")
            if isinstance(node, dict):
                updated = dict(node)
                if uri_update_requested:
                    updated["uri"] = uri
                if name_update_requested:
                    if requested_name:
                        updated["name"] = requested_name
                    else:
                        updated.pop("name", None)
                if enabled_update_requested:
                    updated["enabled"] = requested_enabled
                nodes[index] = updated
            else:
                metadata: dict[str, Any] = {}
                if requested_name:
                    metadata["name"] = requested_name
                if enabled_update_requested:
                    metadata["enabled"] = requested_enabled
                nodes[index] = {"uri": uri, **metadata} if metadata else uri
            if tier == "exit" and enabled_update_requested and not requested_enabled:
                exit_payload = self._tier_payload(payload, "exit")
                _clear_exit_selection_for_nodes(
                    exit_payload,
                    [original_node, nodes[index]],
                    keys=(key, uri),
                    remaining=[entry for other, entry in enumerate(nodes) if other != index],
                )
            elif tier == "exit":
                self._follow_edited_exit_selection(
                    self._tier_payload(payload, "exit"),
                    original_node,
                    nodes[index],
                    index,
                    others=[(other, entry) for other, entry in enumerate(nodes) if other != index],
                )
            return self.write_local(payload)
        raise KeyError(f"{tier} node not found")

    @staticmethod
    def _follow_edited_exit_selection(
        exit_payload: dict[str, Any],
        original_entry: Any,
        updated_entry: Any,
        index: int,
        *,
        others: list[tuple[int, Any]] | tuple[tuple[int, Any], ...] = (),
    ) -> None:
        """An edit of the selected Gateway's own entry keeps it selected.

        Only the saved node's sole carrier moves the selection: the edited
        entry must have been exactly that one node, and no other enabled
        entry may still carry it (a duplicate entry, or a sibling node of a
        multi-node entry, keeps serving the saved node).  Then a new URI
        (rotated credentials, a new server) re-binds the identity to the
        edited node when it parses to exactly one node, and a rename updates
        the label.  Other entries and name-only selections are left alone.
        """

        selection = read_selection(exit_payload)
        if selection.state != "bound":
            return
        try:
            original = exit_nodes_from_entry(original_entry, index, logger=_QUIET_LOGGER)
            edited = exit_nodes_from_entry(updated_entry, index, logger=_QUIET_LOGGER)
        except Exception:
            return
        if len(original) != 1 or gateway_identity(original[0]) != selection.identity:
            return
        if any(
            isinstance(entry, (dict, str))
            and config_entry_enabled(entry)
            and selection.identity in _exit_entry_identities(entry, other_index)
            for other_index, entry in others
        ):
            return
        if len(edited) != 1:
            return
        node = edited[0]
        name = str(node.name or "").strip()
        label = name if name and name != str(original[0].name or "").strip() else selection.label
        write_bound_selection(exit_payload, label, gateway_identity(node))

    def _update_tier_subscription(
        self,
        tier: str,
        key: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        payload = self.read_local()
        entries = self._tier_subscription_urls_payload(payload, tier)
        url_update_requested = any(
            field in updates for field in ("url", "value", "uri")
        )
        requested_url = str(
            updates.get("url", "")
            or updates.get("value", "")
            or updates.get("uri", "")
            or ""
        ).strip()
        if url_update_requested:
            if not requested_url:
                raise ValueError(f"{tier} subscription URL is required")
            validate_url(requested_url)
            if any(
                subscription_entry_url(entry) == requested_url
                and subscription_entry_url(entry) != key
                for entry in entries
            ):
                raise ValueError(f"{tier} subscription already exists")
        enabled_update_requested = "enabled" in updates
        requested_enabled = coerce_bool(updates.get("enabled"), True)

        for index, entry in enumerate(entries):
            if subscription_entry_url(entry) != key:
                continue
            url = requested_url if url_update_requested else subscription_entry_url(entry)
            if not url:
                raise ValueError(f"{tier} subscription URL is required")
            if isinstance(entry, dict):
                updated = dict(entry)
                if url_update_requested:
                    url_field = next(
                        (field for field in ("url", "value", "uri") if field in updated),
                        "url",
                    )
                    updated[url_field] = url
                if enabled_update_requested:
                    updated["enabled"] = requested_enabled
                entries[index] = updated
            elif enabled_update_requested or url_update_requested:
                metadata: dict[str, Any] = {}
                if enabled_update_requested:
                    metadata["enabled"] = requested_enabled
                entries[index] = {"url": url, **metadata}
            else:
                entries[index] = url

            if (
                tier == "exit"
                and enabled_update_requested
                and not requested_enabled
                and coerce_bool(updates.get("_clear_exit_selection"), False)
            ):
                clear_gateway_selection(self._tier_payload(payload, "exit"))
            return self.write_local(payload)
        raise KeyError(f"{tier} subscription not found")

    def _tier1_update_payload(self, updates: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key in ["name", "address", "vless_link", "hysteria2_link", "naive_link"]:
            if key in updates:
                payload[key] = str(updates.get(key, "") or "").strip()
        if "port" in updates:
            payload["port"] = int(str(updates.get("port", "") or "0").strip())
        if "fallback_order" in updates:
            payload["fallback_order"] = [
                str(item).strip()
                for item in updates.get("fallback_order", []) or []
                if str(item).strip()
            ]
        if "protocols" in updates:
            payload["protocols"] = protocols_update_payload(updates.get("protocols"))
        if "enabled" in updates:
            payload["enabled"] = coerce_bool(updates.get("enabled"), True)
        return payload

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError(f"YAML root must be an object: {path}")
        return payload

    def _write_yaml(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        restrict_to_current_user(path.parent)
        data = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
                prefix=f".{path.name}.",
                suffix=".tmp",
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            replace_with_retry(temp_path, path)
            restrict_to_current_user(path)
        finally:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _backup_file(self, path: Path) -> Path | None:
        if not path.exists():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        restrict_to_current_user(self.backup_dir)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = self._copy_unique_backup(path, timestamp)
        self._trim_backups(path.name)
        return backup

    def _copy_unique_backup(self, path: Path, timestamp: str) -> Path:
        for collision_index in range(1000):
            backup = self._backup_path(path.name, timestamp, collision_index)
            try:
                with path.open("rb") as source, backup.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
                shutil.copystat(path, backup)
                restrict_to_current_user(backup)
                return backup
            except FileExistsError:
                continue
        raise FileExistsError(f"could not create unique backup for {path.name}")

    def _backup_path(self, filename: str, timestamp: str, collision_index: int = 0) -> Path:
        collision_suffix = "" if collision_index == 0 else f".{collision_index}"
        return self.backup_dir / f"{filename}.{timestamp}{collision_suffix}.bak"

    def _trim_backups(self, filename: str) -> None:
        backups = sorted(
            self.backup_dir.glob(f"{filename}.*.bak"),
            key=lambda item: (item.stat().st_mtime, item.name),
        )
        overflow = len(backups) - self.max_backups_per_file
        if overflow <= 0:
            return
        for backup in backups[:overflow]:
            try:
                backup.unlink()
            except OSError:
                pass
