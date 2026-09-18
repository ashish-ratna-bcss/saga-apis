"""Covers section 22's exact test cases: only t.me/telegram.me are ever
recognized as Telegram; everything else - including shorteners and unknown
domains - is classified NOT Telegram, and this module never fetches
anything regardless of the outcome."""
import inspect

import pytest

from telegram_app.telegram.discovery.url_classifier import classify_telegram_url, extract_telegram_urls


@pytest.mark.parametrize(
    "url",
    ["https://t.me/channelname", "http://t.me/channelname", "https://www.t.me/channelname", "t.me/channelname"],
)
def test_classifies_t_me_as_telegram(url):
    result = classify_telegram_url(url)
    assert result.is_telegram is True
    assert result.identifier is not None
    assert result.identifier.normalized == "@channelname"


def test_classifies_telegram_me_as_telegram():
    result = classify_telegram_url("https://telegram.me/channelname")
    assert result.is_telegram is True
    assert result.identifier.normalized == "@channelname"


def test_classifies_t_me_invite_hash():
    result = classify_telegram_url("https://t.me/+AbCdEfGhIjK")
    assert result.is_telegram is True
    assert result.identifier.normalized == "https://t.me/+AbCdEfGhIjK"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://bit.ly/example",
        "https://someexternalwebsite.com/page",
        "https://google.com/search?q=x",
        "not a url at all",
    ],
)
def test_classifies_non_telegram_urls_as_not_telegram(url):
    result = classify_telegram_url(url)
    assert result.is_telegram is False
    assert result.identifier is None


def test_never_imports_any_http_client():
    """Structural guarantee, not just a behavioral one: the module must not
    even import an HTTP client, so it is impossible for it to fetch a URL."""
    import telegram_app.telegram.discovery.url_classifier as mod

    source = inspect.getsource(mod)
    for forbidden in ("requests", "aiohttp", "httpx", "urlopen", "urlretrieve"):
        assert forbidden not in source


def test_extract_telegram_urls_from_message_text_ignores_external_links():
    text = (
        "Check this out https://t.me/somechannel and also "
        "https://example.com/page and https://bit.ly/xyz for more info"
    )
    found = extract_telegram_urls(text)

    telegram_hits = [f for f in found if f.is_telegram]
    external_hits = [f for f in found if not f.is_telegram]

    assert len(telegram_hits) == 1
    assert telegram_hits[0].identifier.normalized == "@somechannel"
    assert len(external_hits) == 2


def test_extract_telegram_urls_empty_text_returns_empty_list():
    assert extract_telegram_urls("") == []
    assert extract_telegram_urls(None) == []


def test_extract_telegram_urls_with_no_links_returns_empty_list():
    assert extract_telegram_urls("just some plain text, no links here") == []
