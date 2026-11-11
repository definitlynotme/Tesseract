from models import Node
from tier3_validation_domain import tier3_candidate_config_digest
from tier3_validation_parity_audit import _parity_helper_command


def test_parity_identity_covers_rendered_extra_options():
    first = Node(
        name="first", protocol="tuic", address="127.0.0.1", port=443,
        uuid="00000000-0000-0000-0000-000000000001", password="test",
        extra={"alpn": ["h3"], "skip-cert-verify": True},
    )
    second = first.clone()
    second.extra = dict(first.extra, alpn=["h2"])
    a = _parity_helper_command(first, "https://example.invalid/generate_204", 1)
    b = _parity_helper_command(second, "https://example.invalid/generate_204", 2)
    assert a.config_digest == tier3_candidate_config_digest(first)
    assert b.config_digest == tier3_candidate_config_digest(second)
    assert a.config_digest != b.config_digest
