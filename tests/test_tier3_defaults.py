import unittest

from tier3_defaults import DEFAULT_TIER3_VALIDATED_REPLAY_DAYS
from tier3_sources import bootstrap_source_entries


class Tier3DefaultsTests(unittest.TestCase):
    def test_bootstrap_source_values_are_not_python_defaults(self) -> None:
        self.assertEqual(bootstrap_source_entries(None), [])

    def test_validated_replay_has_independent_thirty_day_default(self) -> None:
        self.assertEqual(DEFAULT_TIER3_VALIDATED_REPLAY_DAYS, 30)


if __name__ == "__main__":
    unittest.main()
