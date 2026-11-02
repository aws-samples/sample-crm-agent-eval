# SPDX-License-Identifier: MIT
"""A local JWKS HTTP server and matching JWT signer, for interceptor integration tests.

Serves a JSON Web Key Set over HTTP on `127.0.0.1` at an OS-assigned port, so `PyJWKClient`
performs a real fetch and `kid` lookup. Use :func:`local_jwks` to start and stop it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.utils import to_base64url_uint


class SigningKey:
    """One RSA keypair identified by a `kid`, published in the served JWKS document."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def sign(self, claims: dict[str, Any], *, kid: str | None = None) -> str:
        """Return a JWT signed by this key.

        `kid` defaults to this key's own identifier but may be overridden to construct a token
        whose header names a `kid` the signature does not actually match, or one absent from the
        served JWKS document entirely.
        """
        payload = json.dumps(claims, separators=(",", ":")).encode("utf-8")
        return pyjwt.PyJWS().encode(
            payload, self.private_key, algorithm="RS256", headers={"kid": kid or self.kid}
        )

    def jwk(self) -> dict[str, str]:
        """Return this key's public half as a JWK dict, in the shape a JWKS document publishes."""
        public_numbers = self.private_key.public_key().public_numbers()
        return {
            "kty": "RSA",
            "kid": self.kid,
            "use": "sig",
            "alg": "RS256",
            "n": to_base64url_uint(public_numbers.n).decode("ascii"),
            "e": to_base64url_uint(public_numbers.e).decode("ascii"),
        }


class JwksServer:
    """A loopback-only HTTP server publishing a mutable set of `SigningKey`s as a JWKS document.

    :meth:`replace_keys` swaps the published set between requests, to exercise `PyJWKClient`'s
    caching and refresh behaviour.
    """

    def __init__(self, keys: list[SigningKey]) -> None:
        self._keys = list(keys)
        self._lock = threading.Lock()
        self.request_count = 0
        self._server = HTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def jwks_url(self) -> str:
        host = str(self._server.server_address[0])
        port = self._server.server_address[1]
        return f"http://{host}:{port}/.well-known/jwks.json"

    def replace_keys(self, keys: list[SigningKey]) -> None:
        """Atomically replace the published key set, simulating a Cognito key rotation."""
        with self._lock:
            self._keys = list(keys)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                """Silence default request logging; the test output should stay quiet."""

            def do_GET(self) -> None:  # noqa: N802
                server.request_count += 1
                with server._lock:
                    keys = list(server._keys)
                body = json.dumps({"keys": [key.jwk() for key in keys]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


@contextmanager
def local_jwks(keys: list[SigningKey]) -> Iterator[JwksServer]:
    """Start a `JwksServer` on loopback for the duration of the `with` block, then tear it down."""
    server = JwksServer(keys)
    server.start()
    try:
        yield server
    finally:
        server.stop()
