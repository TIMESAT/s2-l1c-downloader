"""Secure in-memory OAuth token handling for official CDSE downloads."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import requests

from .utils import AuthenticationError


class TokenManager:
    """Acquire and refresh CDSE bearer tokens without persisting credentials or tokens."""

    def __init__(
        self,
        token_url: str,
        *,
        timeout: int = 120,
        environment: Mapping[str, str] | None = None,
        session_factory: Callable[[], requests.Session] = requests.Session,
    ) -> None:
        self.token_url = token_url
        self.timeout = timeout
        self._environment = environment if environment is not None else os.environ
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._access_token = self._environment.get("CDSE_ACCESS_TOKEN", "").strip()
        self._refresh_token = ""
        self._expires_at = float("inf") if self._access_token else 0.0
        self._manual_token_invalidated = False

    def invalidate(self) -> None:
        """Discard a rejected access token so the next call refreshes or reauthenticates."""
        with self._lock:
            self._access_token = ""
            self._expires_at = 0.0
            self._manual_token_invalidated = True

    def get_token(self) -> str:
        with self._lock:
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            if self._refresh_token:
                try:
                    return self._request_token(
                        {
                            "client_id": "cdse-public",
                            "grant_type": "refresh_token",
                            "refresh_token": self._refresh_token,
                        }
                    )
                except AuthenticationError:
                    self._refresh_token = ""
            username = self._environment.get("CDSE_USERNAME", "").strip()
            password = self._environment.get("CDSE_PASSWORD", "")
            if not username or not password:
                suffix = (
                    " The supplied CDSE_ACCESS_TOKEN was rejected or expired."
                    if self._manual_token_invalidated
                    else ""
                )
                raise AuthenticationError(
                    "CDSE download credentials are unavailable. Export CDSE_USERNAME and "
                    f"CDSE_PASSWORD (and CDSE_TOTP when applicable), or a short-lived "
                    f"CDSE_ACCESS_TOKEN.{suffix}"
                )
            form = {
                "client_id": "cdse-public",
                "grant_type": "password",
                "username": username,
                "password": password,
            }
            totp = self._environment.get("CDSE_TOTP", "").strip()
            if totp:
                form["totp"] = totp
            return self._request_token(form)

    def _request_token(self, form: dict[str, str]) -> str:
        session = self._session_factory()
        try:
            response = session.post(self.token_url, data=form, timeout=self.timeout)
            if response.status_code != 200:
                detail = ""
                try:
                    payload = response.json()
                    detail = str(payload.get("error_description") or payload.get("error") or "")
                except (ValueError, AttributeError):
                    pass
                message = f"CDSE authentication failed with HTTP {response.status_code}"
                if detail:
                    message = f"{message}: {detail}"
                raise AuthenticationError(message)
            try:
                payload: dict[str, Any] = response.json()
            except ValueError as error:
                raise AuthenticationError("CDSE authentication returned invalid JSON") from error
            access_token = str(payload.get("access_token", ""))
            if not access_token:
                raise AuthenticationError("CDSE authentication response contained no access token")
            self._access_token = access_token
            self._refresh_token = str(payload.get("refresh_token", self._refresh_token))
            expires_in = max(1, int(payload.get("expires_in", 600)))
            self._expires_at = time.monotonic() + max(1, expires_in - 30)
            return self._access_token
        except requests.RequestException as error:
            raise AuthenticationError(
                f"Could not reach the CDSE identity service: {error}"
            ) from error
        finally:
            session.close()
