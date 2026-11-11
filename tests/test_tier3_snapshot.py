import unittest

from tier3_snapshot import tier3_snapshot_metadata


class Tier3SnapshotTests(unittest.TestCase):
    def test_snapshot_metadata_preserves_tier3_runtime_fields(self) -> None:
        collection = {"source_count": 3}
        controller = {"target_alive": 5}

        metadata = tier3_snapshot_metadata(
            source_count=3,
            last_collection_at="2026-05-31T12:00:00Z",
            enabled=True,
            level=2,
            level_reason="below target",
            collection_report=collection,
            controller=controller,
            revival_count=1,
        )

        self.assertEqual(
            metadata,
            {
                "tier": "tier3",
                "source_count": 3,
                "last_collection_at": "2026-05-31T12:00:00Z",
                "enabled": True,
                "level": 2,
                "level_reason": "below target",
                "collection_ref": {
                    "kind": "tier3_source_state_ref",
                    "runtime_generation": 0,
                    "report_revision": 0,
                    "pool_revision": 0,
                },
                "pool_state": {},
                "controller": controller,
                "revival_count": 1,
            },
        )
        self.assertNotIn("collection", metadata)
        self.assertIsNot(metadata["controller"], controller)


if __name__ == "__main__":
    unittest.main()
