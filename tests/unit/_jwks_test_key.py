# SPDX-License-Identifier: MIT
"""An RSA test key, a loopback JWKS server, and Cognito-shaped claims, for unit tests.

Tokens from :func:`sign` verify through `scripts.smoke.verified_claims` against
:func:`jwks_url`, a real HTTP JWKS document served on `127.0.0.1` for the life of the process, so
`PyJWKClient` performs a genuine fetch and `kid` lookup. :func:`local_jwks` serves an arbitrary key
set for a single test instead.
"""

from __future__ import annotations

import functools
import json
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Final

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.utils import to_base64url_uint
from scripts.smoke.verified_claims import USE_ACCESS, USE_ID, cognito_issuer

REGION: Final = "us-east-1"
USER_POOL_ID: Final = "us-east-1_TestPool"
CLIENT_ID: Final = "test-spa-client"
DEFAULT_KID: Final = "unit-test-key"
#: Lifetime of the default claims' `exp`; longer than any suite run, so import-time tokens hold.
TOKEN_LIFETIME_SECONDS: Final = 24 * 3600


class TestKey:
    """One RSA keypair identified by `kid`."""

    __test__ = False

    def __init__(self, kid: str = DEFAULT_KID) -> None:
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def sign(self, claims: dict[str, Any], *, kid: str | None = None) -> str:
        """Return an RS256 JWT over `claims`, its header naming `kid` (default: this key's)."""
        payload = json.dumps(claims, separators=(",", ":")).encode("utf-8")
        return jwt.PyJWS().encode(
            payload, self.private_key, algorithm="RS256", headers={"kid": kid or self.kid}
        )

    def jwk(self) -> dict[str, str]:
        """Return the public half as a JWK."""
        numbers = self.private_key.public_key().public_numbers()
        return {
            "kty": "RSA",
            "kid": self.kid,
            "use": "sig",
            "alg": "RS256",
            "n": to_base64url_uint(numbers.n).decode("ascii"),
            "e": to_base64url_uint(numbers.e).decode("ascii"),
        }


class _JwksServer:
    def __init__(self, keys: Sequence[TestKey]) -> None:
        body = json.dumps({"keys": [key.jwk() for key in keys]}).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                """Keep test output quiet."""

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host!s}:{port}/.well-known/jwks.json"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@contextmanager
def local_jwks(keys: Sequence[TestKey]) -> Iterator[str]:
    """Serve `keys` as a JWKS document for the `with` block; yields its URL."""
    server = _JwksServer(keys)
    try:
        yield server.url
    finally:
        server.stop()


@functools.cache
def default_key() -> TestKey:
    """The process-wide key that :func:`jwks_url` publishes."""
    return TestKey()


@functools.cache
def jwks_url() -> str:
    """URL of a process-lifetime JWKS document publishing :func:`default_key`."""
    return _JwksServer([default_key()]).url


def sign(claims: dict[str, Any], *, kid: str | None = None) -> str:
    """Sign `claims` with :func:`default_key`."""
    return default_key().sign(claims, kid=kid)


def id_token_claims(**overrides: Any) -> dict[str, Any]:
    """Valid Cognito ID-token claims for `USER_POOL_ID`/`CLIENT_ID`, updated by `overrides`."""
    claims: dict[str, Any] = {
        "iss": cognito_issuer(REGION, USER_POOL_ID),
        "aud": CLIENT_ID,
        "token_use": USE_ID,
        "sub": "00000000-0000-0000-0000-000000000000",
        "exp": int(time.time()) + TOKEN_LIFETIME_SECONDS,
    }
    claims.update(overrides)
    return claims


def access_token_claims(**overrides: Any) -> dict[str, Any]:
    """Valid Cognito access-token claims for `USER_POOL_ID`/`CLIENT_ID`, updated by `overrides`."""
    claims: dict[str, Any] = {
        "iss": cognito_issuer(REGION, USER_POOL_ID),
        "client_id": CLIENT_ID,
        "token_use": USE_ACCESS,
        "sub": "00000000-0000-0000-0000-000000000000",
        "exp": int(time.time()) + TOKEN_LIFETIME_SECONDS,
    }
    claims.update(overrides)
    return claims


def id_token(**overrides: Any) -> str:
    """A signed, valid ID token carrying `overrides`."""
    return sign(id_token_claims(**overrides))


def access_token(**overrides: Any) -> str:
    """A signed, valid access token carrying `overrides`."""
    return sign(access_token_claims(**overrides))
