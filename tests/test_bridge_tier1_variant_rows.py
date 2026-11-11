"""§8u (CLAUDE_CHANGES): a multi-protocol Vanguard node is one nodes.list row."""

import unittest

from bridge_nodes import _merge_protocol_variants


def dto(node_id, name, protocol, status="failed", *, enabled=True, latency=None, slow=False,
        tier="vanguard", kind="manual", checked=None):
    return {"id": node_id, "name": name, "tier": tier, "kind": kind, "enabled": enabled,
            "status": status, "reason_code": "connection_timeout" if status == "failed" else None,
            "protocol": protocol, "latency_ms": latency, "subscription_id": None, "slow": slow,
            "last_checked_at_ms": checked}


def raw(group=None, rank=0, name="edge", key=""):
    if group is None:
        return {}
    return {"variant_group": group, "variant_rank": rank, "variant_name": name, "variant_key": key}


class Tier1VariantRowsTest(unittest.TestCase):
    def merge(self, pairs):
        return _merge_protocol_variants([p[0] for p in pairs], [p[1] for p in pairs], lambda v: v)

    def test_variants_become_one_row_in_fallback_order(self):
        rows = self.merge([
            (dto("a" * 24, "edge :: VMess", "vmess", checked=5), raw("g" * 24, 2, key="vmess")),
            (dto("b" * 24, "edge :: SOCKS5", "socks5", checked=9), raw("g" * 24, 0, key="socks5")),
            (dto("c" * 24, "other", "trojan"), raw()),
            (dto("d" * 24, "edge :: VLESS Reality", "vless", checked=7), raw("g" * 24, 1, key="vless_reality")),
        ])
        self.assertEqual([r["name"] for r in rows], ["edge", "other"])
        merged = rows[0]
        self.assertEqual(merged["id"], "b" * 24)  # first variant in fallback order
        self.assertEqual(merged["protocol"], "socks5 / vless_reality / vmess")
        self.assertEqual(merged["status"], "failed")
        self.assertEqual(merged["last_checked_at_ms"], 9)

    def test_any_usable_variant_makes_the_node_usable(self):
        rows = self.merge([
            (dto("b" * 24, "edge :: SOCKS5", "socks5"), raw("g" * 24, 0)),
            (dto("d" * 24, "edge :: VLESS", "vless", "healthy", latency=80, slow=True), raw("g" * 24, 1)),
        ])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["id"], row["status"], row["latency_ms"], row["slow"], row["reason_code"]),
                         ("b" * 24, "healthy", 80, True, None))

    def test_two_physical_nodes_and_other_tiers_stay_apart(self):
        rows = self.merge([
            (dto("a" * 24, "x :: SOCKS5", "socks5"), raw("1" * 24, 0, name="x")),
            (dto("b" * 24, "y :: SOCKS5", "socks5"), raw("2" * 24, 0, name="y")),
            (dto("c" * 24, "s1", "vmess", tier="sentinel", kind="subscription"), raw("3" * 24, 0)),
        ])
        self.assertEqual([r["name"] for r in rows], ["x", "y", "s1"])

    def test_disabled_node_stays_disabled(self):
        rows = self.merge([
            (dto("a" * 24, "x :: SOCKS5", "socks5", "disabled", enabled=False), raw("1" * 24, 0, name="x")),
            (dto("b" * 24, "x :: VMess", "vmess", "disabled", enabled=False), raw("1" * 24, 1, name="x")),
        ])
        self.assertEqual((rows[0]["enabled"], rows[0]["status"]), (False, "disabled"))


if __name__ == "__main__":
    unittest.main()
