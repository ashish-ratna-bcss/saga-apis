import pytest

from telegram_app.telegram.discovery import IdentifierKind, normalize_identifier


@pytest.mark.parametrize(
    "raw,expected_kind,expected_value",
    [
        ("@ExampleChannel", IdentifierKind.USERNAME, "ExampleChannel"),
        ("examplechannel", IdentifierKind.USERNAME, "examplechannel"),
        ("https://t.me/examplechan", IdentifierKind.USERNAME, "examplechan"),
        ("t.me/examplechan", IdentifierKind.USERNAME, "examplechan"),
        ("https://t.me/+AbCdEfGhIjK", IdentifierKind.INVITE_HASH, "AbCdEfGhIjK"),
        ("t.me/joinchat/xyz123", IdentifierKind.INVITE_HASH, "xyz123"),
        ("-1001234567890", IdentifierKind.NUMERIC_ID, "-1001234567890"),
        ("123456", IdentifierKind.NUMERIC_ID, "123456"),
    ],
)
def test_normalize_identifier_kinds(raw, expected_kind, expected_value):
    result = normalize_identifier(raw)
    assert result.kind == expected_kind
    assert result.value == expected_value


def test_normalized_username_is_reparseable():
    first = normalize_identifier("@ExampleChannel")
    second = normalize_identifier(first.normalized)
    assert second.kind == IdentifierKind.USERNAME
    assert second.normalized == first.normalized


def test_normalized_invite_hash_is_reparseable():
    first = normalize_identifier("t.me/+AbCdEf")
    second = normalize_identifier(first.normalized)
    assert second.kind == IdentifierKind.INVITE_HASH
    assert second.value == "AbCdEf"


def test_empty_identifier_rejected():
    with pytest.raises(ValueError):
        normalize_identifier("   ")


def test_invalid_username_rejected():
    with pytest.raises(ValueError):
        normalize_identifier("@a")  # too short
