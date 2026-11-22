# SPDX-License-Identifier: MIT
"""Per-call output-token limits shared by the live provider adapters.

:data:`TURN_OUTPUT_TOKEN_CAP` is the output-token ceiling every adapter sends on each model call
(Anthropic ``max_tokens`` / OpenAI ``max_output_tokens``). It must not exceed any configured
model's ``max_output_tokens`` in ``config/model_capabilities.yaml``.
"""

from __future__ import annotations

from typing import Final

TURN_OUTPUT_TOKEN_CAP: Final[int] = 16_384
