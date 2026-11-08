# SPDX-License-Identifier: MIT
"""Suite-wide stub for the ``apigw`` channel's app-client allowlist parameter.

For the whole session :mod:`crm.envelope` reads its allowlist from a stub that admits
:data:`TEST_API_CLIENT_ID` and :data:`RECORDED_EVENT_CLIENT_ID`, so an API Gateway event carrying
``token_use="id"`` and ``aud=TEST_API_CLIENT_ID``, or built from a recorded event under
``tests/fixtures/events/``, decodes without SSM. A test that requests
:func:`apigw_client_allowlist` opts out: it gets its own stub and an empty cache for its duration.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from crm import envelope

#: The app client id the stub allowlist parameter admits.
TEST_API_CLIENT_ID = "spa-client-test"

#: The ``client_id`` of the access token in the recorded API Gateway events under
#: ``tests/fixtures/events/``.
RECORDED_EVENT_CLIENT_ID = "cognito-app-client-id"

#: The parameter name the stub environment points `crm.envelope` at.
TEST_API_CLIENT_IDS_PARAMETER = "/crm-test/api-allowed-client-ids"


@dataclass
class StubAllowlist:
    """A stub SSM parameter: `value` is returned for every read and each read name is recorded."""

    value: str = f"{TEST_API_CLIENT_ID},{RECORDED_EVENT_CLIENT_ID}"
    reads: list[str] = field(default_factory=list)

    def read(self, name: str) -> str:
        self.reads.append(name)
        return self.value


SESSION_ALLOWLIST = StubAllowlist()


@pytest.fixture(scope="session", autouse=True)
def _session_apigw_client_allowlist() -> Iterator[StubAllowlist]:
    """Point :mod:`crm.envelope` at :data:`SESSION_ALLOWLIST` for the session, fixtures included."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(envelope.API_ALLOWED_CLIENT_IDS_PARAMETER_ENV, TEST_API_CLIENT_IDS_PARAMETER)
        patch.setattr(envelope, "_parameter_reader", SESSION_ALLOWLIST.read)
        patch.setattr(envelope, "_allowed_client_ids_cache", None)
        yield SESSION_ALLOWLIST


@pytest.fixture
def apigw_client_allowlist(monkeypatch: pytest.MonkeyPatch) -> StubAllowlist:
    """Opt out of the session stub: a fresh stub reader and an empty cache for this test only."""
    stub = StubAllowlist()
    monkeypatch.setenv(envelope.API_ALLOWED_CLIENT_IDS_PARAMETER_ENV, TEST_API_CLIENT_IDS_PARAMETER)
    monkeypatch.setattr(envelope, "_allowed_client_ids_cache", None)
    monkeypatch.setattr(envelope, "_parameter_reader", stub.read)
    return stub
