from __future__ import annotations

import pytest

from s2l1c.auth import TokenManager
from s2l1c.utils import AuthenticationError


class TokenResponse:
    status_code = 200

    def __init__(self, access_token, refresh_token):
        self.payload = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 600,
        }

    def json(self):
        return self.payload


class AuthSession:
    def __init__(self, responses, calls):
        self.responses = responses
        self.calls = calls

    def post(self, url, data, timeout):
        self.calls.append((url, data.copy(), timeout))
        return self.responses.pop(0)

    def close(self):
        pass


def test_token_manager_reauthenticates_with_in_memory_refresh_token():
    responses = [TokenResponse("first", "refresh"), TokenResponse("second", "refresh-2")]
    calls = []
    manager = TokenManager(
        "https://identity.dataspace.copernicus.eu/token",
        environment={"CDSE_USERNAME": "researcher", "CDSE_PASSWORD": "local-secret"},
        session_factory=lambda: AuthSession(responses, calls),
    )

    assert manager.get_token() == "first"
    assert calls[0][1]["grant_type"] == "password"
    manager.invalidate()
    assert manager.get_token() == "second"
    assert calls[1][1] == {
        "client_id": "cdse-public",
        "grant_type": "refresh_token",
        "refresh_token": "refresh",
    }


def test_token_manager_requires_local_credentials():
    manager = TokenManager("https://identity.dataspace.copernicus.eu/token", environment={})
    with pytest.raises(AuthenticationError, match="credentials are unavailable"):
        manager.get_token()
