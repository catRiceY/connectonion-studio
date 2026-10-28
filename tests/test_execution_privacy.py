"""Synthetic credentials only; no real history, .env or key lookups."""

import pytest

from co_studio.execution_privacy import REDACTED, redact_text, redact_value


@pytest.mark.parametrize(("text", "expected"), [
    ("api_key=fake-key-123", "api_key=[REDACTED]"),
    ('{"api_key": "fake key 123", "ok": true}', '{"api_key": "[REDACTED]", "ok": true}'),
    ("'access_token': 'fake-token-123'", "'access_token': '[REDACTED]'"),
    ('password="fake\\\"password"; retry', 'password="[REDACTED]"; retry'),
    ("export OPENAI_API_KEY='fake-provider-key'", "export OPENAI_API_KEY='[REDACTED]'"),
    ("DB_PASSWORD=fake-db-pass\nstatus=ok", "DB_PASSWORD=[REDACTED]\nstatus=ok"),
    ("secret: fake-secret, order_id: 123", "secret: [REDACTED], order_id: 123"),
    ('{"message": "password=fake-password"}', '{"message": "password=[REDACTED]"}'),
    ("Authorization: Bearer fake-bearer-token\nstatus: ok", "Authorization: [REDACTED]\nstatus: ok"),
    ('"Authorization": "Bearer fake-bearer-token"', '"Authorization": "[REDACTED]"'),
    ("authorization=Basic ZmFrZTp0ZXN0", "authorization=[REDACTED]"),
    ("Proxy-Authorization: Bearer fake-proxy-token", "Proxy-Authorization: [REDACTED]"),
    ("Use Bearer fake-bearer-token to retry.", "Use Bearer [REDACTED] to retry."),
    ("The key is sk-proj-fake0123456789; failed.", "The key is [REDACTED]; failed."),
])
def test_redacts_recognizable_credentials_in_readable_text(text, expected):
    assert redact_text(text) == expected
    assert redact_text(expected) == expected


@pytest.mark.parametrize("label", [
    "PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "DSA PRIVATE KEY",
    "OPENSSH PRIVATE KEY", "ENCRYPTED PRIVATE KEY",
])
def test_redacts_complete_pem_private_keys_without_erasing_surrounding_text(label):
    text = f"Before\n-----BEGIN {label}-----\nFAKE-KEY-DATA\n-----END {label}-----\nAfter"
    assert redact_text(text) == "Before\n[REDACTED]\nAfter"


def test_redacts_unterminated_private_key_through_end_of_string():
    assert redact_text("Key:\n-----BEGIN PRIVATE KEY-----\nFAKE-DATA") == "Key:\n[REDACTED]"


@pytest.mark.parametrize("key", [
    "api_key", "API-KEY", "apiKey", "api key", "access_token", "refreshToken",
    "password", "passwd", "client-secret", "secret", "PRIVATE_KEY", "cookie",
    "Set-Cookie", "Authorization", "invite_code", "OPENAI_API_KEY", "DB_PASSWORD",
])
def test_recognizes_normalized_structured_secret_keys(key):
    assert redact_value({key: {"value": "fake-secret"}}) == {key: REDACTED}


def test_json_projection_recurses_without_mutating_original():
    original = {
        "result": [
            {"headers": {"Authorization": "Bearer fake-secret"}, "message": "Works"},
            "password=fake-password; done",
        ],
        "usage": {"input_tokens": 12, "output_tokens": 3},
        "order_id": "order-001", "ok": True, "optional": None, "ratio": 0.75,
    }
    projected = redact_value(original)
    assert projected == {
        "result": [
            {"headers": {"Authorization": REDACTED}, "message": "Works"},
            "password=[REDACTED]; done",
        ],
        "usage": {"input_tokens": 12, "output_tokens": 3},
        "order_id": "order-001", "ok": True, "optional": None, "ratio": 0.75,
    }
    assert projected is not original
    assert projected["result"] is not original["result"]
    assert original["result"][0]["headers"]["Authorization"] == "Bearer fake-secret"
    projected["usage"]["input_tokens"] = 99
    assert original["usage"]["input_tokens"] == 12


def test_preserves_nonsecret_sentences_token_metrics_and_public_keys():
    ordinary = (
        "Token budget=1200; input_tokens=20, output_tokens=5; order_id=order-123. "
        "Explain password hashing and secret management. Bearer tokens are supported. "
        "-----BEGIN PUBLIC KEY-----\nFAKE-PUBLIC-DATA\n-----END PUBLIC KEY-----"
    )
    assert redact_text(ordinary) == ordinary
    assert redact_value({"tokens": 25, "token_count": 25, "token_budget": 1200}) == {
        "tokens": 25, "token_count": 25, "token_budget": 1200,
    }


def test_unlabelled_ordinary_strings_are_not_guaranteed_secret_detection():
    # Explicit limitation: content alone cannot establish whether this is a
    # password, order reference or harmless text. No real key lookup is made.
    assert redact_text("unlabelled-example-value") == "unlabelled-example-value"
