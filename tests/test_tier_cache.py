import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from models import Node, NodeStatus
from tier_cache import (
    Tier3ObservationFact,
    Tier3ObservationKind,
    TierObservationCache,
)


def make_node(name: str, status: NodeStatus = NodeStatus.ALIVE, *, tier: str = "tier3") -> Node:
    return Node(
        name=name,
        protocol="ss",
        uri=f"ss://{name}",
        address=f"{name}.example.com",
        port=8388,
        tier=tier,
        status=status,
    )


class TierObservationCacheTests(unittest.TestCase):
    def test_observation_payload_excludes_process_local_scheduler_keys(self) -> None:
        keys = (
            "tier3_source_key", "tier3_source_arm_key", "tier3_source_arm",
            "tier3_source_coarse_key", "tier3_source_diversity_key",
            "tier3_source_root_key", "tier3_source_source_root_key",
        )
        node = make_node("validated")
        node.extra.update({key: "previous-process-hmac" for key in keys})
        node.extra.update(source_root_id="opaque-authority", source_unit_id="opaque-unit",
                          tier3_admission_authority="auto_admission")
        cache = TierObservationCache(Path(".pytest-work/unused-projection.json"))
        extra = cache._stable_node_payload(node)["extra"]
        for key in keys:
            self.assertNotIn(key, extra)
        self.assertEqual(extra["source_root_id"], "opaque-authority")
        self.assertEqual(extra["source_unit_id"], "opaque-unit")
        self.assertEqual(extra["tier3_admission_authority"], "auto_admission")
        self.assertEqual(node.extra["tier3_source_key"], "previous-process-hmac")
        record = {"node": node.to_dict(), "last_mixed_port_pass_at": "2026-09-09T00:00:00Z",
                  "mixed_port_pass_count": 4}
        migrated = cache._normalize_v3_record(record)
        self.assertEqual(migrated["node"]["extra"], extra)
        self.assertEqual(migrated["mixed_port_pass_count"], 4)
        self.assertEqual(migrated["last_mixed_port_pass_at"], record["last_mixed_port_pass_at"])
        self.assertEqual(record["node"]["extra"]["tier3_source_key"], "previous-process-hmac")

    def test_stable_tier3_snapshot_uses_exact_recent_auto_pool_configs(self) -> None:
        with self.subTest(snapshot="workspace-fixed-temp"):
            path = (
                Path.cwd()
                / ".pytest-work"
                / "tier-cache-stable-observation.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            self.addCleanup(path.unlink, missing_ok=True)
            now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

            alive = make_node("alive")
            alive.extra["tier3_origin"] = "tier3b"
            slow = make_node("slow", NodeStatus.SLOW)
            slow.extra["tier3_origin"] = "tier3b"
            expired = make_node("expired")
            expired.extra["tier3_origin"] = "tier3b"
            failed_newer = make_node("failed-newer")
            failed_newer.extra["tier3_origin"] = "tier3b"
            user_pool = make_node("user-pool")
            user_pool.extra["tier3_origin"] = "tier3a"

            for validated in (alive, slow, expired, failed_newer):
                validated.extra.update(
                    {
                        "tier3_validation_purpose": "auto_admission",
                        "tier3_validator_clean_confirmed": True,
                        "tier3_admission_commit_attempted": True,
                        "validated_by": "tier3_validator_helper",
                        "validation_profile": "endpoint_process_helper",
                    }
                )

            def record(node: Node, observed_at: datetime) -> dict:
                return {
                    "identity": "|".join(["tier3", *node.dedupe_key]),
                    "tier": "tier3",
                    "status": (
                        node.status.value
                        if isinstance(node.status, NodeStatus)
                        else str(node.status)
                    ),
                    "last_healthy_at": observed_at.isoformat(),
                    "last_observed_at": observed_at.isoformat(),
                    "node": node.to_dict(),
                }

            failed_healthy_at = now - timedelta(hours=2)
            payload = {
                "schema_version": 2,
                "generated_at": now.isoformat(),
                "nodes": [
                    record(alive, now - timedelta(hours=1)),
                    record(slow, now - timedelta(hours=1)),
                    record(expired, now - timedelta(days=8)),
                    record(failed_newer, failed_healthy_at),
                    record(user_pool, now - timedelta(hours=1)),
                ],
                "negative": [
                    {
                        "identity": "|".join(
                            ["tier3", *failed_newer.dedupe_key]
                        ),
                        "tier": "tier3",
                        "status": NodeStatus.DEAD.value,
                        "last_failed_at": (
                            failed_healthy_at + timedelta(minutes=5)
                        ).isoformat(),
                        "failure_stage": "tcp_dead",
                    }
                ],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            cache = TierObservationCache(path)

            snapshot = cache.stable_tier3_observation_snapshot(
                days=7,
                config_digest_provider=lambda node: node.address,
                now=now,
            )

            self.assertEqual(snapshot.classification_for(alive.address), "validated_positive")
            self.assertEqual(snapshot.classification_for(slow.address), "validated_positive")
            self.assertEqual(
                snapshot.classification_for(expired.address),
                "observed_nonpositive",
            )
            self.assertEqual(
                snapshot.classification_for(failed_newer.address),
                "validated_reprobe",
            )
            self.assertEqual(snapshot.classification_for(user_pool.address), "unknown")
            self.assertEqual(snapshot.classification_for("changed.example.com"), "unknown")
            self.assertEqual(snapshot.exact_positive_record_count, 2)
            self.assertEqual(snapshot.validated_reprobe_record_count, 1)
            self.assertEqual(snapshot.expired_record_count, 1)
            self.assertEqual(snapshot.newer_negative_excluded_count, 0)
            diagnostic = snapshot.to_diagnostic_dict()
            self.assertNotIn("identity", json.dumps(diagnostic))
            self.assertNotIn(alive.address, json.dumps(diagnostic))

    def test_schema_v2_migration_separates_validated_tcp_only_and_unverified(
        self,
    ) -> None:
        path = Path.cwd() / ".pytest-work" / "tier-cache-v3-migration.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        records: list[dict] = []
        trusted_count = 25
        tcp_only_count = 1827

        for index in range(trusted_count + tcp_only_count + 1):
            node = make_node(f"legacy-{index}")
            node.extra["tier3_origin"] = "tier3b"
            if index < trusted_count:
                node.extra.update(
                    {
                        "tier3_validation_purpose": "auto_admission",
                        "tier3_validator_clean_confirmed": True,
                        "tier3_admission_commit_enqueued": True,
                        "validated_by": "tier3_validator_helper",
                        "validation_profile": "endpoint_process_helper",
                    }
                )
            elif index < trusted_count + tcp_only_count:
                node.extra.update(
                    {
                        "tier3_tcp_prefilter_observed": True,
                        "tier3_tcp_prefilter_responsive": True,
                    }
                )
            records.append(
                {
                    "identity": "|".join(["tier3", *node.dedupe_key]),
                    "tier": "tier3",
                    "status": NodeStatus.ALIVE.value,
                    "last_healthy_at": now.isoformat(),
                    "last_observed_at": now.isoformat(),
                    "observed_count": 1,
                    "node": node.to_dict(),
                }
            )

        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "generated_at": now.isoformat(),
                    "nodes": records,
                    "negative": [],
                }
            ),
            encoding="utf-8",
        )
        cache = TierObservationCache(path, history_limit=3000)

        snapshot = cache.stable_tier3_observation_snapshot(
            days=7,
            config_digest_provider=lambda node: node.address,
            now=now,
        )

        self.assertEqual(snapshot.validated_positive_record_count, trusted_count)
        self.assertEqual(snapshot.tcp_only_record_count, tcp_only_count)
        self.assertEqual(
            snapshot.classification_for("legacy-0.example.com"),
            "validated_positive",
        )
        self.assertEqual(
            snapshot.classification_for(
                f"legacy-{trusted_count}.example.com"
            ),
            "tcp_only",
        )
        self.assertEqual(
            snapshot.classification_for(
                f"legacy-{trusted_count + tcp_only_count}.example.com"
            ),
            "observed_nonpositive",
        )

        cache.observe_tier3_facts(
            [
                Tier3ObservationFact(
                    node=make_node("persist-v3"),
                    kind=Tier3ObservationKind.TCP_RESPONSIVE,
                )
            ]
        )
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], 3)
        migrated_tcp = next(
            item
            for item in persisted["nodes"]
            if item.get("node", {}).get("address")
            == f"legacy-{trusted_count}.example.com"
        )
        self.assertNotIn("last_healthy_at", migrated_tcp)
        self.assertEqual(migrated_tcp["observation_authority"], "tcp_only")

    def test_censored_candidates_never_become_validated_positive(self) -> None:
        path = Path.cwd() / ".pytest-work" / "tier-cache-v3-censored.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        cache = TierObservationCache(path, history_limit=2000)
        censored = [make_node(f"censored-{index}") for index in range(1258)]
        passed = [make_node(f"passed-{index}") for index in range(25)]
        for node in [*censored, *passed]:
            node.extra["tier3_origin"] = "tier3b"

        cache.observe_tier3_facts(
            [
                *[
                    Tier3ObservationFact(
                        node=node,
                        kind=Tier3ObservationKind.RIGHT_CENSORED,
                    )
                    for node in censored
                ],
                *[
                    Tier3ObservationFact(
                        node=node,
                        kind=Tier3ObservationKind.MIXED_PORT_PASS,
                    )
                    for node in passed
                ],
            ]
        )
        snapshot = cache.stable_tier3_observation_snapshot(
            days=7,
            config_digest_provider=lambda node: node.address,
        )

        self.assertEqual(snapshot.validated_positive_record_count, 25)
        self.assertEqual(snapshot.tcp_only_record_count, 0)
        self.assertEqual(
            snapshot.classification_for("censored-0.example.com"),
            "unknown",
        )
        self.assertEqual(
            snapshot.classification_for("passed-0.example.com"),
            "validated_positive",
        )

    def test_schema_v2_migration_recovers_failure_order_from_record_itself(
        self,
    ) -> None:
        path = Path.cwd() / ".pytest-work" / "tier-cache-v3-record-order.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

        def legacy_record(name: str, *, stage: str, status: NodeStatus) -> dict:
            node = make_node(name, status)
            node.extra.update(
                {
                    "tier3_origin": "tier3b",
                    "tier3_validation_purpose": "auto_admission",
                    "tier3_validator_clean_confirmed": True,
                    "tier3_admission_commit_attempted": True,
                    "validated_by": "tier3_validator_helper",
                    "validation_profile": "endpoint_process_helper",
                    "tier3_failure_stage": stage,
                }
            )
            return {
                "identity": "|".join(["tier3", *node.dedupe_key]),
                "tier": "tier3",
                "status": status.value,
                "last_healthy_at": (now - timedelta(minutes=2)).isoformat(),
                "last_observed_at": now.isoformat(),
                "observed_count": 2,
                "node": node.to_dict(),
            }

        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "generated_at": now.isoformat(),
                    "nodes": [
                        legacy_record(
                            "later-dead",
                            stage="tcp_dead",
                            status=NodeStatus.DEAD,
                        ),
                        legacy_record(
                            "later-censored",
                            stage="validation_deferred",
                            status=NodeStatus.SUSPICIOUS,
                        ),
                    ],
                    # Old bounded negative ledgers could already have dropped
                    # the matching entry, so migration must use the row too.
                    "negative": [],
                }
            ),
            encoding="utf-8",
        )

        snapshot = TierObservationCache(path).stable_tier3_observation_snapshot(
            days=7,
            config_digest_provider=lambda node: node.address,
            now=now,
        )

        self.assertEqual(
            snapshot.classification_for("later-dead.example.com"),
            "validated_reprobe",
        )
        self.assertEqual(
            snapshot.classification_for("later-censored.example.com"),
            "validated_positive",
        )

    def test_censor_preserves_pass_but_newer_deterministic_failure_excludes_it(
        self,
    ) -> None:
        path = Path.cwd() / ".pytest-work" / "tier-cache-v3-ordering.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        cache = TierObservationCache(path)
        node = make_node("ordering")
        node.extra["tier3_origin"] = "tier3b"
        pass_at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        censor_at = pass_at + timedelta(minutes=1)
        fail_at = pass_at + timedelta(minutes=2)

        cache.observe_tier3_facts(
            [
                Tier3ObservationFact(
                    node=node,
                    kind=Tier3ObservationKind.MIXED_PORT_PASS,
                    observed_at=pass_at.isoformat(),
                ),
                Tier3ObservationFact(
                    node=node,
                    kind=Tier3ObservationKind.RIGHT_CENSORED,
                    observed_at=censor_at.isoformat(),
                ),
            ]
        )
        snapshot = cache.stable_tier3_observation_snapshot(
            days=7,
            config_digest_provider=lambda candidate: candidate.address,
            now=censor_at,
        )
        self.assertEqual(
            snapshot.classification_for(node.address),
            "validated_positive",
        )

        failed = node.clone()
        failed.status = NodeStatus.DEAD
        failed.suspicious_reason = "mihomo_validation_failed: body mismatch"
        cache.observe_tier3_facts(
            [
                Tier3ObservationFact(
                    node=failed,
                    kind=(
                        Tier3ObservationKind.DETERMINISTIC_VALIDATION_FAIL
                    ),
                    observed_at=fail_at.isoformat(),
                )
            ]
        )
        snapshot = cache.stable_tier3_observation_snapshot(
            days=7,
            config_digest_provider=lambda candidate: candidate.address,
            now=fail_at,
        )
        self.assertEqual(
            snapshot.classification_for(node.address),
            "observed_nonpositive",
        )

    def test_validated_replay_is_source_authorized_and_tcp_dead_is_reprobe(
        self,
    ) -> None:
        path = Path.cwd() / ".pytest-work" / "tier-cache-validated-replay.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        cache = TierObservationCache(path)
        authority = "subscription_url:https://source.example/sub"
        removed_authority = "subscription_url:https://removed.example/sub"
        pass_at = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

        def replay_node(name: str, source_authority: str, contract: str) -> Node:
            node = make_node(name)
            node.extra.update(
                {
                    "tier3_origin": "tier3b",
                    "tier3_source_root_id": source_authority,
                    "tier3_validation_contract_hash": contract,
                }
            )
            return node

        positive = replay_node("positive", authority, "contract-current")
        reprobe_tcp = replay_node("reprobe-tcp", authority, "contract-current")
        reprobe_contract = replay_node("reprobe-contract", authority, "contract-old")
        deterministic = replay_node(
            "deterministic-fail",
            authority,
            "contract-current",
        )
        removed = replay_node("removed-source", removed_authority, "contract-current")
        cache.observe_tier3_facts(
            [
                Tier3ObservationFact(
                    node=node,
                    kind=Tier3ObservationKind.MIXED_PORT_PASS,
                    observed_at=pass_at.isoformat(),
                )
                for node in (
                    positive,
                    reprobe_tcp,
                    reprobe_contract,
                    deterministic,
                    removed,
                )
            ]
        )
        cache.observe_tier3_facts(
            [
                Tier3ObservationFact(
                    node=reprobe_tcp,
                    kind=Tier3ObservationKind.TCP_DEAD,
                    observed_at=(pass_at + timedelta(seconds=1)).isoformat(),
                ),
                Tier3ObservationFact(
                    node=deterministic,
                    kind=Tier3ObservationKind.DETERMINISTIC_VALIDATION_FAIL,
                    observed_at=(pass_at + timedelta(seconds=2)).isoformat(),
                ),
            ]
        )

        snapshot = cache.validated_tier3_replay_snapshot(
            days=7,
            limit=64,
            configured_source_authorities=(authority,),
            config_digest_provider=lambda node: node.address,
            validation_contract_hash="contract-current",
            now=pass_at + timedelta(minutes=1),
        )
        by_digest = {
            candidate.config_digest: candidate
            for candidate in snapshot.candidates
        }

        self.assertEqual(
            by_digest[positive.address].visibility_class,
            "validated_positive",
        )
        self.assertEqual(
            by_digest[reprobe_tcp.address].visibility_class,
            "validated_reprobe",
        )
        self.assertTrue(by_digest[reprobe_tcp.address].tcp_reprobe_required)
        self.assertEqual(
            by_digest[reprobe_contract.address].visibility_class,
            "validated_reprobe",
        )
        self.assertFalse(
            by_digest[reprobe_contract.address].contract_matches
        )
        self.assertNotIn(deterministic.address, by_digest)
        self.assertNotIn(removed.address, by_digest)
        self.assertEqual(snapshot.deterministic_failure_excluded_count, 1)
        self.assertEqual(snapshot.source_authority_excluded_count, 1)
        diagnostic = json.dumps(snapshot.to_diagnostic_dict())
        self.assertNotIn(positive.address, diagnostic)
        self.assertNotIn(authority, diagnostic)

    def test_validated_replay_splits_fresh_and_historical_trust_layers(
        self,
    ) -> None:
        path = Path.cwd() / ".pytest-work" / "tier-cache-replay-horizons.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        cache = TierObservationCache(path)
        authority = "subscription_url:https://authority.example/sub"
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

        def cached(name: str, contract: str = "contract-current") -> Node:
            node = make_node(name)
            node.extra.update(
                {
                    "tier3_origin": "tier3b",
                    "tier3_source_root_id": authority,
                    "tier3_validation_contract_hash": contract,
                }
            )
            return node

        fresh = cached("fresh")
        aged = cached("aged")
        changed = cached("changed", "contract-old")
        tcp_dead = cached("tcp-dead")
        cache.observe_tier3_facts(
            [
                Tier3ObservationFact(
                    node=fresh,
                    kind=Tier3ObservationKind.MIXED_PORT_PASS,
                    observed_at=(now - timedelta(days=6)).isoformat(),
                ),
                Tier3ObservationFact(
                    node=aged,
                    kind=Tier3ObservationKind.MIXED_PORT_PASS,
                    observed_at=(now - timedelta(days=8)).isoformat(),
                ),
                Tier3ObservationFact(
                    node=changed,
                    kind=Tier3ObservationKind.MIXED_PORT_PASS,
                    observed_at=(now - timedelta(hours=1)).isoformat(),
                ),
                Tier3ObservationFact(
                    node=tcp_dead,
                    kind=Tier3ObservationKind.MIXED_PORT_PASS,
                    observed_at=(now - timedelta(hours=2)).isoformat(),
                ),
                Tier3ObservationFact(
                    node=tcp_dead,
                    kind=Tier3ObservationKind.TCP_DEAD,
                    observed_at=(now - timedelta(hours=1)).isoformat(),
                ),
            ]
        )

        snapshot = cache.validated_tier3_replay_snapshot(
            days=30,
            fresh_days=7,
            limit=64,
            configured_source_authorities=(authority,),
            config_digest_provider=lambda node: node.address,
            validation_contract_hash="contract-current",
            now=now,
        )
        by_digest = {
            item.config_digest: item for item in snapshot.candidates
        }

        self.assertEqual(
            by_digest[fresh.address].trust_class,
            "fresh_validated",
        )
        for node in (aged, changed, tcp_dead):
            self.assertEqual(
                by_digest[node.address].trust_class,
                "historical_reprobe",
            )
        self.assertEqual(snapshot.candidates[0].config_digest, fresh.address)
        self.assertEqual(snapshot.replay_days, 30)
        self.assertEqual(snapshot.fresh_days, 7)

    def test_tcp_only_flood_cannot_evict_authoritative_pass_history(self) -> None:
        cache = TierObservationCache(
            Path.cwd() / ".pytest-work" / "tier-cache-retention-only.json",
            history_limit=5000,
        )
        pass_at = "2026-08-01T00:00:00+00:00"
        records: dict[str, dict] = {
            "trusted": {
                "identity": "trusted",
                "tier": "tier3",
                "last_mixed_port_pass_at": pass_at,
                "last_observed_at": pass_at,
            }
        }
        for index in range(20_000):
            observed_at = (
                datetime(2026, 8, 2, tzinfo=timezone.utc)
                + timedelta(seconds=index)
            ).isoformat()
            identity = f"tcp-only-{index}"
            records[identity] = {
                "identity": identity,
                "tier": "tier3",
                "last_tcp_responsive_at": observed_at,
                "last_observed_at": observed_at,
            }

        retained = cache._prune_records(records)

        self.assertEqual(len(retained), 5000)
        self.assertIn("trusted", retained)
        self.assertEqual(
            retained["trusted"]["last_mixed_port_pass_at"],
            pass_at,
        )

    def test_tcp_dead_cannot_hide_newer_deterministic_failure_from_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json")
            node = make_node("ordered-negative", NodeStatus.ALIVE)
            node.extra.update(
                {
                    "source_type": "subscription_url",
                    "tier3_source_root_id": "subscription:https://source.invalid",
                }
            )
            base = datetime.now(timezone.utc) - timedelta(minutes=1)
            cache.observe_tier3_facts(
                [
                    Tier3ObservationFact(
                        node=node,
                        kind=Tier3ObservationKind.MIXED_PORT_PASS,
                        observed_at=base.isoformat(),
                    ),
                    Tier3ObservationFact(
                        node=node,
                        kind=(
                            Tier3ObservationKind.DETERMINISTIC_VALIDATION_FAIL
                        ),
                        observed_at=(base + timedelta(seconds=1)).isoformat(),
                    ),
                    Tier3ObservationFact(
                        node=node,
                        kind=Tier3ObservationKind.TCP_DEAD,
                        observed_at=(base + timedelta(seconds=2)).isoformat(),
                    ),
                ]
            )

            kept, skipped = cache.filter_recently_failed_tier3(
                [node],
                cooldown_hours=6,
            )

            self.assertEqual(kept, [])
            self.assertEqual(skipped, 1)

    def test_repeated_negative_filter_uses_one_file_snapshot_until_external_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.json"
            candidate = make_node("cached-bad", NodeStatus.DEAD)
            identity = "|".join(["tier3", *candidate.dedupe_key])
            payload = {
                "schema_version": 2,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "nodes": [],
                "negative": [
                    {
                        "identity": identity,
                        "tier": "tier3",
                        "status": NodeStatus.DEAD.value,
                        "last_failed_at": datetime.now(timezone.utc).isoformat(),
                        "failure_count": 1,
                        "failure_stage": "deterministic_validation_fail",
                    }
                ],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            cache = TierObservationCache(path)

            with mock.patch.object(
                cache,
                "_read_payload_from_disk",
                wraps=cache._read_payload_from_disk,
            ) as read_payload:
                for _ in range(50):
                    kept, skipped = cache.filter_recently_failed_tier3(
                        [candidate],
                        cooldown_hours=6,
                    )
                    self.assertEqual(kept, [])
                    self.assertEqual(skipped, 1)
                self.assertEqual(read_payload.call_count, 1)

                payload["negative"] = []
                path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                kept, skipped = cache.filter_recently_failed_tier3(
                    [candidate],
                    cooldown_hours=6,
                )

                self.assertEqual(read_payload.call_count, 2)
                self.assertEqual(kept, [candidate])
                self.assertEqual(skipped, 0)

    def test_revive_tier3_returns_only_evicted_previous_healthy_tier3_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json")
            evicted = Node(
                name="free-evicted",
                protocol="ss",
                uri="ss://one",
                address="free.example.com",
                port=8388,
                tier="tier3",
                status=NodeStatus.ALIVE,
            )
            cache.observe("tier3", [evicted, make_node("still-in-pool")])
            evicted.status = NodeStatus.SUSPICIOUS
            evicted.suspicious_reason = "mihomo_validation_failed: timeout"
            evicted.extra["tier3_evicted_from_pool"] = True
            cache.observe("tier3", [evicted])
            cache.observe(
                "tier2",
                [
                    Node(
                        name="commercial",
                        protocol="ss",
                        uri="ss://two",
                        address="commercial.example.com",
                        port=8388,
                        tier="tier2",
                        status=NodeStatus.ALIVE,
                    )
                ],
            )

            result = cache.revive_tier3(days=7, limit=10)

            self.assertEqual(len(result.candidates), 1)
            self.assertEqual(result.candidates[0].name, "free-evicted")
            self.assertEqual(result.candidates[0].status, NodeStatus.UNKNOWN)
            self.assertTrue(result.candidates[0].extra["revived_from_cache"])
            self.assertNotIn("tier3_evicted_from_pool", result.candidates[0].extra)

    def test_revive_tier3_ignores_tier3a_user_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json")
            tier3a = make_node("user-imported")
            tier3a.extra["tier3_origin"] = "tier3a"
            tier3b = make_node("auto")
            tier3b.extra["tier3_origin"] = "tier3b"

            cache.observe("tier3", [tier3a, tier3b])
            tier3a.status = NodeStatus.SUSPICIOUS
            tier3a.extra["tier3_evicted_from_pool"] = True
            tier3b.status = NodeStatus.SUSPICIOUS
            tier3b.extra["tier3_evicted_from_pool"] = True
            cache.observe("tier3", [tier3a, tier3b])
            result = cache.revive_tier3(days=7, limit=10)

            self.assertEqual([node.name for node in result.candidates], ["auto"])

    def test_non_tier3_observations_are_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json")

            cache.observe("tier1", [make_node("vanguard", tier="tier1")])
            cache.observe("tier2", [make_node("sentinel", tier="tier2")])
            cache.observe("exit", [make_node("gateway", tier="exit")])

            payload = cache.load()
            self.assertEqual(payload["nodes"], [])
            self.assertEqual(payload["negative"], [])

    def test_next_tier3_write_prunes_legacy_non_tier3_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "generated_at": "2026-06-01T00:00:00+00:00",
                        "nodes": [
                            {
                                "identity": "tier2|ss|tier2.example.com|8388",
                                "tier": "tier2",
                                "status": NodeStatus.ALIVE.value,
                                "last_observed_at": "2026-06-01T00:00:00+00:00",
                                "node": make_node("sentinel", tier="tier2").to_dict(),
                            },
                            {
                                "identity": "tier3|ss|tier3.example.com|8388",
                                "tier": "tier3",
                                "status": NodeStatus.ALIVE.value,
                                "last_observed_at": "2026-06-01T00:00:00+00:00",
                                "node": make_node("bastion").to_dict(),
                            },
                        ],
                        "negative": [
                            {
                                "identity": "tier2|ss|bad-tier2.example.com|8388",
                                "tier": "tier2",
                                "status": NodeStatus.DEAD.value,
                                "last_failed_at": "2026-06-01T00:00:00+00:00",
                                "failure_count": 1,
                            },
                            {
                                "identity": "tier3|ss|bad-tier3.example.com|8388",
                                "tier": "tier3",
                                "status": NodeStatus.DEAD.value,
                                "last_failed_at": "2026-06-01T00:00:00+00:00",
                                "failure_count": 1,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            cache = TierObservationCache(path)

            cache.observe("tier3", [make_node("fresh")])

            payload = cache.load()
            self.assertEqual({item["tier"] for item in payload["nodes"]}, {"tier3"})
            self.assertEqual({item["tier"] for item in payload["negative"]}, {"tier3"})
            self.assertFalse(any(str(item["identity"]).startswith("tier2|") for item in payload["nodes"]))
            self.assertFalse(any(str(item["identity"]).startswith("tier2|") for item in payload["negative"]))

    def test_observe_coalesces_repeated_status_for_same_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json")
            node = make_node("stable")

            for _ in range(5):
                cache.observe("tier3", [node])

            payload = cache.load()
            self.assertEqual(len(payload["nodes"]), 1)
            self.assertEqual(payload["nodes"][0]["status"], NodeStatus.ALIVE.value)
            self.assertEqual(payload["nodes"][0]["observed_count"], 5)

    def test_dead_node_is_evicted_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json", dead_evict_threshold=3)
            node = make_node("dead")
            cache.observe("tier3", [node])
            node.status = NodeStatus.DEAD

            cache.observe("tier3", [node])
            cache.observe("tier3", [node])
            self.assertEqual(len(cache.load()["nodes"]), 1)

            cache.observe("tier3", [node])
            self.assertEqual(cache.load()["nodes"], [])

    def test_first_dead_validation_failure_only_enters_negative_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json", dead_evict_threshold=2)
            node = make_node("still-dead", NodeStatus.DEAD)

            cache.observe("tier3", [node])
            payload = cache.load()
            self.assertEqual(payload["nodes"], [])
            self.assertEqual(len(payload["negative"]), 1)
            self.assertEqual(payload["negative"][0]["status"], NodeStatus.DEAD.value)

    def test_batch_mihomo_failure_does_not_enter_negative_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json", dead_evict_threshold=2)
            node = make_node("batch-failed", NodeStatus.SUSPICIOUS)
            node.suspicious_reason = "mihomo_batch_validation_failed: proxy delay failed"

            cache.observe("tier3", [node])

            payload = cache.load()
            self.assertEqual(payload["nodes"], [])
            self.assertEqual(payload["negative"], [])

    def test_mihomo_503_is_not_persisted_and_tcp_history_is_reprobe_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.json"
            cache = TierObservationCache(path, dead_evict_threshold=2)
            transient = make_node("batch-503", NodeStatus.SUSPICIOUS)
            transient.suspicious_reason = "mihomo_batch_validation_failed: HTTP Error 503: Service Unavailable"
            transient.source = "https://source.example/sub"
            transient.extra["source_type"] = "subscription_url"
            transient.extra["tier3_outside_lane"] = "tier3b_candidate"
            transient_502 = make_node("batch-502", NodeStatus.SUSPICIOUS)
            transient_502.suspicious_reason = "mihomo_batch_validation_failed: HTTP Error 502: Bad Gateway"
            transient_502.source = "https://source.example/sub"
            transient_502.extra["source_type"] = "subscription_url"
            dead = make_node("tcp-dead", NodeStatus.DEAD)
            dead.suspicious_reason = "tcp connection timed out"
            dead.source = "https://source.example/sub"
            dead.extra["source_type"] = "subscription_url"

            cache.observe("tier3", [transient, transient_502, dead])
            payload = cache.load()
            stale_failed_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
            for record in payload["negative"]:
                record["last_failed_at"] = stale_failed_at
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            kept, skipped = cache.filter_recently_failed_tier3([transient, transient_502, dead], cooldown_hours=6)
            records = {item["identity"]: item for item in cache.load()["negative"]}

            self.assertEqual(kept, [transient, transient_502, dead])
            self.assertEqual(skipped, 0)
            dead_record = records["|".join(["tier3", *dead.dedupe_key])]
            self.assertNotIn("|".join(["tier3", *transient.dedupe_key]), records)
            self.assertNotIn("|".join(["tier3", *transient_502.dedupe_key]), records)
            self.assertEqual(dead_record["failure_stage"], "tcp_dead")

    def test_transient_validator_negative_records_are_migrated_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "observations.json"
            cache = TierObservationCache(path, dead_evict_threshold=2)
            transient = make_node("old-transient", NodeStatus.SUSPICIOUS)
            transient.suspicious_reason = "candidate_transient_503: HTTP Error 503"
            transient.extra["tier3_failure_stage"] = "candidate_transient_503"
            deferred = make_node("old-deferred", NodeStatus.SUSPICIOUS)
            deferred.suspicious_reason = "validation_deferred: deadline"
            deferred.extra["tier3_failure_stage"] = "validation_deferred"
            facility = make_node("old-facility", NodeStatus.SUSPICIOUS)
            facility.suspicious_reason = "validator_facility_failure: controller failed"
            facility.extra["tier3_failure_stage"] = "validator_facility_failure"
            all_probe = make_node("old-all-probe", NodeStatus.SUSPICIOUS)
            all_probe.suspicious_reason = "full_validation_all_probe_transient: all probes timed out"
            all_probe.extra["tier3_failure_stage"] = "full_validation_all_probe_transient"
            delay_failed = make_node("old-delay-failed", NodeStatus.SUSPICIOUS)
            delay_failed.suspicious_reason = "mihomo_batch_validation_failed: proxy delay failed"
            delay_failed.extra["tier3_failure_stage"] = "mihomo_delay_failed"
            dead = make_node("old-dead", NodeStatus.DEAD)
            dead.suspicious_reason = "tcp connection timed out"
            dead.extra["tier3_failure_stage"] = "tcp_dead"

            cache.observe("tier3", [transient, deferred, facility, all_probe, delay_failed, dead])
            payload = cache.load()
            stale_failed_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
            for record in payload["negative"]:
                record["last_failed_at"] = stale_failed_at
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            kept, skipped = cache.filter_recently_failed_tier3(
                [transient, deferred, facility, all_probe, delay_failed, dead],
                cooldown_hours=6,
            )
            records = {item["identity"]: item for item in cache.load()["negative"]}

            self.assertEqual(
                {node.name for node in kept},
                {
                    "old-transient",
                    "old-deferred",
                    "old-facility",
                    "old-all-probe",
                    "old-delay-failed",
                    "old-dead",
                },
            )
            self.assertEqual(skipped, 0)
            self.assertNotIn("|".join(["tier3", *transient.dedupe_key]), records)
            self.assertNotIn("|".join(["tier3", *deferred.dedupe_key]), records)
            self.assertNotIn("|".join(["tier3", *facility.dedupe_key]), records)
            self.assertNotIn("|".join(["tier3", *delay_failed.dedupe_key]), records)
            self.assertNotIn("|".join(["tier3", *all_probe.dedupe_key]), records)
            self.assertEqual(records["|".join(["tier3", *dead.dedupe_key])]["failure_stage"], "tcp_dead")

    def test_dead_observation_does_not_restart_after_eviction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json", dead_evict_threshold=2)
            node = make_node("was-good")
            cache.observe("tier3", [node])
            node.status = NodeStatus.DEAD

            cache.observe("tier3", [node])
            self.assertEqual(cache.load()["nodes"][0]["dead_streak"], 1)
            cache.observe("tier3", [node])
            self.assertEqual(cache.load()["nodes"], [])
            cache.observe("tier3", [node])

            payload = cache.load()
            self.assertEqual(payload["nodes"], [])
            self.assertEqual(len(payload["negative"]), 1)
            self.assertEqual(payload["negative"][0]["failure_count"], 3)

    def test_pruning_keeps_healthy_records_before_dead_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json", history_limit=2, dead_evict_threshold=10)

            cache.observe("tier3", [make_node("alive-one"), make_node("alive-two"), make_node("dead", NodeStatus.DEAD)])

            payload = cache.load()
            self.assertEqual({item["status"] for item in payload["nodes"]}, {NodeStatus.ALIVE.value})
            self.assertEqual(len(payload["nodes"]), 2)

    def test_revived_node_is_marked_and_skipped_during_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json", revive_cooldown_hours=24)
            node = make_node("cached")
            cache.observe("tier3", [node])
            node.status = NodeStatus.SUSPICIOUS
            node.suspicious_reason = "mihomo_validation_failed: timeout"
            node.extra["tier3_evicted_from_pool"] = True
            cache.observe("tier3", [node])

            first = cache.revive_tier3(days=7, limit=10)
            second = cache.revive_tier3(days=7, limit=10)

            self.assertEqual(len(first.candidates), 1)
            self.assertEqual(first.candidates[0].extra["cache_revive_count"], 1)
            self.assertEqual(second.candidates, [])
            self.assertEqual(second.skipped_recently, 1)

    def test_identity_survives_display_name_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json")
            node = Node(
                name="free",
                protocol="tuic",
                uri="",
                address="same.example.com",
                port=443,
                tier="tier3",
                uuid="33333333-3333-3333-3333-333333333333",
                password="pass",
                status=NodeStatus.ALIVE,
            )
            cache.observe("tier3", [node])
            node.name = "free-decorated"
            cache.observe("tier3", [node])

            payload = cache.load()

            self.assertEqual(len(payload["nodes"]), 1)
            self.assertEqual(payload["nodes"][0]["observed_count"], 2)

    def test_recent_failed_tier3_candidate_is_filtered_during_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json")
            node = make_node("bad", NodeStatus.SUSPICIOUS)
            node.suspicious_reason = "mihomo_validation_failed: timeout"
            cache.observe("tier3", [node])

            kept, skipped = cache.filter_recently_failed_tier3([node], cooldown_hours=6)

            self.assertEqual(kept, [])
            self.assertEqual(skipped, 1)

            kept, skipped = cache.filter_recently_failed_tier3(
                [node],
                cooldown_hours=6,
                exempt_identities={"|".join(["tier3", *node.dedupe_key])},
            )

            self.assertEqual(kept, [node])
            self.assertEqual(skipped, 0)

    def test_first_validation_failure_only_enters_negative_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json")
            node = make_node("first-bad", NodeStatus.SUSPICIOUS)
            node.suspicious_reason = "mihomo_validation_failed: timeout"

            cache.observe("tier3", [node])
            payload = cache.load()

            self.assertEqual(payload["nodes"], [])
            self.assertEqual(len(payload["negative"]), 1)
            self.assertEqual(payload["negative"][0]["status"], NodeStatus.SUSPICIOUS.value)

    def test_uncacheable_first_suspicious_failure_does_not_enter_main_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json")
            node = make_node("first-bad", NodeStatus.SUSPICIOUS)
            node.suspicious_reason = "validation_exception: local executor stopped"

            with mock.patch.object(cache, "_write_records") as write_records:
                cache.observe("tier3", [node])
            write_records.assert_not_called()
            payload = cache.load()

            self.assertEqual(payload["nodes"], [])
            self.assertEqual(payload["negative"], [])

    def test_durable_node_payload_omits_per_validation_transaction_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json")
            node = make_node("stable", NodeStatus.ALIVE)
            node.extra.update(
                {
                    "tier3_source_root_id": "root-a",
                    "tier3_probe_attempt_facts": [{"attempt_id": "secret-attempt"}],
                    "tier3_candidate_dispatch_ticket": {"ticket_id": "ticket-a"},
                    "tier3_validator_lease_id": "lease-a",
                    "validation_probe_urls_attempted": ["https://probe.invalid"],
                }
            )

            cache.observe("tier3", [node])
            payload = cache.load()

            extra = payload["nodes"][0]["node"]["extra"]
            self.assertEqual(extra["tier3_source_root_id"], "root-a")
            self.assertNotIn("tier3_probe_attempt_facts", extra)
            self.assertNotIn("tier3_candidate_dispatch_ticket", extra)
            self.assertNotIn("tier3_validator_lease_id", extra)
            self.assertNotIn("validation_probe_urls_attempted", extra)

    def test_repeated_noop_observation_reuses_primary_and_negative_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json")
            healthy = make_node("indexed", NodeStatus.ALIVE)
            cache.observe("tier3", [healthy])
            transient = make_node("transient", NodeStatus.SUSPICIOUS)
            transient.suspicious_reason = "validation_exception: executor stopped"

            with (
                mock.patch.object(
                    cache,
                    "_records_from_payload",
                    wraps=cache._records_from_payload,
                ) as primary_index,
                mock.patch.object(
                    cache,
                    "_negative_records_from_payload",
                    wraps=cache._negative_records_from_payload,
                ) as negative_index,
            ):
                for _ in range(50):
                    cache.observe("tier3", [transient])

            primary_index.assert_not_called()
            negative_index.assert_not_called()

    def test_previously_healthy_validation_failure_stays_observed_for_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json")
            node = make_node("was-good", NodeStatus.ALIVE)
            cache.observe("tier3", [node])
            node.status = NodeStatus.SUSPICIOUS
            node.suspicious_reason = "mihomo_validation_failed: timeout"

            cache.observe("tier3", [node])
            payload = cache.load()

            self.assertEqual(len(payload["nodes"]), 1)
            self.assertEqual(payload["nodes"][0]["status"], NodeStatus.SUSPICIOUS.value)
            self.assertEqual(payload["nodes"][0]["observed_count"], 2)
            self.assertEqual(len(payload["negative"]), 1)
            self.assertEqual(cache.revive_tier3(days=7, limit=10).candidates, [])

    def test_evicted_failed_node_stays_revival_eligible_after_dead_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json", dead_evict_threshold=2)
            node = make_node("evicted", NodeStatus.ALIVE)
            cache.observe("tier3", [node])
            node.status = NodeStatus.DEAD
            node.extra["tier3_evicted_from_pool"] = True

            cache.observe("tier3", [node])
            cache.observe("tier3", [node])
            payload = cache.load()
            result = cache.revive_tier3(days=7, limit=10)

            self.assertEqual(len(payload["nodes"]), 1)
            self.assertTrue(payload["nodes"][0]["evicted_from_pool"])
            self.assertEqual([candidate.name for candidate in result.candidates], ["evicted"])

    def test_negative_records_are_pruned_to_history_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = TierObservationCache(Path(temp_dir) / "observations.json", history_limit=3)

            for index in range(10):
                node = make_node(f"bad-{index}", NodeStatus.SUSPICIOUS)
                node.suspicious_reason = "mihomo_validation_failed: timeout"
                cache.observe("tier3", [node])

            payload = cache.load()

            self.assertEqual(len(payload["negative"]), 3)


if __name__ == "__main__":
    unittest.main()
