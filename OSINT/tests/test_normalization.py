import pytest

from osint_app.enums import IdentifierType
from osint_app.normalization import (
    NormalizationError,
    detect_identifier_type,
    normalize_domain,
    normalize_email,
    normalize_identifier,
    normalize_phone,
    normalize_username,
)


def test_normalize_phone_indian_with_spaces():
    assert normalize_phone("+91 98765 43210") == "+919876543210"


def test_normalize_phone_local_format_defaults_to_india():
    assert normalize_phone("098765 43210") == "+919876543210"


def test_normalize_phone_invalid_raises():
    with pytest.raises(NormalizationError):
        normalize_phone("12345")


def test_normalize_email_lowercases_and_strips():
    assert normalize_email("John@Example.COM") == "john@example.com"


def test_normalize_email_invalid_raises():
    with pytest.raises(NormalizationError):
        normalize_email("not-an-email")


def test_normalize_username_strips_at_and_lowercases():
    assert normalize_username("@Rahul_123") == "rahul_123"


def test_normalize_domain_from_url():
    assert normalize_domain("https://www.example.com/path?x=1") == "example.com"


def test_normalize_domain_bare():
    assert normalize_domain("EXAMPLE.com") == "example.com"


def test_normalize_domain_preserves_non_www_leading_w():
    assert normalize_domain("wowexample.com") == "wowexample.com"


def test_detect_identifier_type_email():
    assert detect_identifier_type("a@b.com") == IdentifierType.EMAIL


def test_detect_identifier_type_phone():
    assert detect_identifier_type("+91 98765 43210") == IdentifierType.PHONE


def test_detect_identifier_type_domain():
    assert detect_identifier_type("example.com") == IdentifierType.DOMAIN


def test_detect_identifier_type_domain_with_www_prefix():
    assert detect_identifier_type("www.example.com") == IdentifierType.DOMAIN


def test_detect_identifier_type_domain_not_mangled_by_leading_w_chars():
    # a domain starting with "ww" (not the literal "www.") must not be
    # corrupted by naive lstrip("www.")-style prefix stripping
    assert detect_identifier_type("wwexample.com") == IdentifierType.DOMAIN


def test_detect_identifier_type_username():
    assert detect_identifier_type("rahul_123") == IdentifierType.USERNAME


def test_detect_identifier_type_person_name():
    assert detect_identifier_type("Rahul Kumar") == IdentifierType.PERSON_NAME


def test_normalize_identifier_auto_detects():
    itype, value = normalize_identifier("+91 98765 43210")
    assert itype == IdentifierType.PHONE
    assert value == "+919876543210"


def test_normalize_identifier_empty_raises():
    with pytest.raises(NormalizationError):
        normalize_identifier("   ")
