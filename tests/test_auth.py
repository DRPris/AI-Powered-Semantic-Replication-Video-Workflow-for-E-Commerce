from types import SimpleNamespace

from harness.auth import authorize_api_request, extract_api_key, parse_api_keys


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


def test_parse_api_keys_ignores_empty_and_placeholder_values():
    assert parse_api_keys(" alpha, ,your_internal_api_key_here,beta ") == [
        "alpha",
        "beta",
    ]


def test_extract_api_key_prefers_x_api_key():
    headers = Headers(
        {
            "x-api-key": "from-header",
            "authorization": "Bearer from-bearer",
        }
    )
    assert extract_api_key(headers) == "from-header"


def test_authorize_request_accepts_valid_bearer_token():
    settings = SimpleNamespace(API_AUTH_ENABLED=True, API_KEYS="key-one,key-two")
    decision = authorize_api_request(
        settings,
        Headers({"authorization": "Bearer key-two"}),
    )

    assert decision.allowed is True


def test_authorize_request_rejects_missing_or_invalid_key():
    settings = SimpleNamespace(API_AUTH_ENABLED=True, API_KEYS="expected")

    missing = authorize_api_request(settings, Headers({}))
    invalid = authorize_api_request(settings, Headers({"x-api-key": "wrong"}))

    assert missing.allowed is False
    assert missing.status_code == 401
    assert invalid.allowed is False
    assert invalid.status_code == 401


def test_authorize_request_fails_closed_when_enabled_without_keys():
    settings = SimpleNamespace(API_AUTH_ENABLED=True, API_KEYS="")
    decision = authorize_api_request(settings, Headers({"x-api-key": "anything"}))

    assert decision.allowed is False
    assert decision.status_code == 503


def test_authorize_request_can_be_disabled_for_local_development():
    settings = SimpleNamespace(API_AUTH_ENABLED=False, API_KEYS="")
    decision = authorize_api_request(settings, Headers({}))

    assert decision.allowed is True
