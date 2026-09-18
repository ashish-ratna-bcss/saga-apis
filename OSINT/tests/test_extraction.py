from osint_app.enums import IdentifierType
from osint_app.extraction import (
    extract_all,
    extract_domain_from_url,
    extract_emails,
    extract_phones,
    extract_usernames_from_url,
    is_generic_domain,
)


def test_extract_emails_finds_real_email():
    candidates = extract_emails("Contact rahul.kumar@example.com for details")
    assert len(candidates) == 1
    assert candidates[0].value == "rahul.kumar@example.com"
    assert candidates[0].identifier_type == IdentifierType.EMAIL
    assert candidates[0].confidence == 0.75


def test_extract_emails_ignores_plain_text():
    assert extract_emails("no email here at all") == []


def test_extract_phones_finds_valid_indian_number():
    candidates = extract_phones("call +91 98765 43210 now")
    assert len(candidates) == 1
    assert candidates[0].value == "+919876543210"
    assert candidates[0].identifier_type == IdentifierType.PHONE


def test_extract_phones_ignores_short_digit_sequences():
    # a product SKU / order number should not be treated as a phone number
    candidates = extract_phones("order id 12345")
    assert candidates == []


def test_extract_usernames_from_known_profile_url():
    candidates = extract_usernames_from_url("https://github.com/torvalds")
    assert len(candidates) == 1
    assert candidates[0].value == "torvalds"
    assert candidates[0].confidence == 0.6


def test_extract_usernames_rejects_reserved_path_segments():
    assert extract_usernames_from_url("https://github.com/login") == []
    assert extract_usernames_from_url("https://twitter.com/search") == []


def test_extract_usernames_ignores_unrecognized_site():
    assert extract_usernames_from_url("https://example.com/torvalds") == []


def test_extract_domain_from_url_strips_www_and_path():
    assert extract_domain_from_url("https://www.somecompany.co.in/contact-us?x=1") == "somecompany.co.in"


def test_extract_domain_from_url_handles_garbage():
    assert extract_domain_from_url("not a url") is None


def test_is_generic_domain():
    assert is_generic_domain("github.com") is True
    assert is_generic_domain("GITHUB.com") is True
    assert is_generic_domain("somecompany.co.in") is False


def test_extract_all_combines_text_and_url_extractors():
    candidates = extract_all("reach me at a@b.com", url="https://github.com/someuser")
    types = {c.identifier_type for c in candidates}
    assert IdentifierType.EMAIL in types
    assert IdentifierType.USERNAME in types


def test_extract_phones_never_raises_on_pathological_input():
    # regression guard: extraction must degrade to "no candidates", not crash a job
    extract_phones("(" * 10000)
    extract_emails("@" * 10000)
