# SPDX-License-Identifier: MIT
"""The vendor-SDK and AWS-SDK loggers held at WARNING, with no SDK or HTTP-client dependency.

Importable from any process, Lambda zips included. :mod:`providers.sdk_transport` re-exports both
names and quiets the loggers when imported.
"""

from __future__ import annotations

import logging
from typing import Final

#: Loggers held at WARNING so no SDK or transport debug record (which carries URLs and bodies) is
#: ever emitted.
SDK_LOGGERS: Final[tuple[str, ...]] = (
    "anthropic",
    "openai",
    "httpx2",
    "httpcore2",
    "botocore",
    "boto3",
    "urllib3",
    "s3transfer",
)


def quiet_sdk_loggers() -> None:
    """Set every logger in :data:`SDK_LOGGERS` to WARNING.

    Runs at :mod:`providers.sdk_transport` import, at every client build and after the Runtime's
    logging setup, since importing a vendor SDK with ``ANTHROPIC_LOG`` / ``OPENAI_LOG`` set lowers
    its logger's level.
    """
    for name in SDK_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
