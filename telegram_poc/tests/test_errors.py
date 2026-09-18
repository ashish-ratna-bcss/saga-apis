"""translate_telegram_error must map AuthKeyUnregisteredError (and the other
SESSION_INVALID_ERRORS) to a distinct TELEGRAM_SESSION_INVALID code - never
the generic TELEGRAM_RPC_ERROR bucket - matching the API shape mandated for
an invalid Telegram session."""
from telethon.errors import AuthKeyUnregisteredError, ChannelPrivateError, SessionRevokedError

from telegram_app.telegram.errors import translate_telegram_error


def test_auth_key_unregistered_maps_to_session_invalid():
    err = translate_telegram_error(AuthKeyUnregisteredError(None))

    assert err.code == "TELEGRAM_SESSION_INVALID"
    assert err.retryable is False
    assert err.http_status == 401


def test_session_invalid_payload_matches_required_shape():
    err = translate_telegram_error(AuthKeyUnregisteredError(None))

    assert err.to_payload() == {
        "error": {
            "code": "TELEGRAM_SESSION_INVALID",
            "message": err.message,
            "retryable": False,
        }
    }


def test_other_session_invalid_errors_also_map_to_session_invalid():
    err = translate_telegram_error(SessionRevokedError(None))
    assert err.code == "TELEGRAM_SESSION_INVALID"


def test_channel_private_is_unaffected():
    """Sanity check: SESSION_INVALID_ERRORS handling must not swallow
    unrelated per-channel outcomes."""
    err = translate_telegram_error(ChannelPrivateError(None))
    assert err.code == "CHANNEL_PRIVATE"
    assert err.code != "TELEGRAM_SESSION_INVALID"
