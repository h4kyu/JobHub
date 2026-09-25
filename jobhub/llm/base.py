"""LLM backend protocol. Only the Claude Code (subscription) backend is implemented today."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence


#: Per-call sentinel: omit --model so headless Claude uses the user's own Claude Code default.
#: (A per-call `model=None` instead means "fall back to whatever model the backend was built with".)
CLAUDE_DEFAULT = "__claude_code_default__"


class LLMError(RuntimeError):
    pass


class LLMRateLimitError(LLMError):
    """Usage/rate limit hit; the run should back off and stop cleanly."""


@dataclass
class LLMResult:
    data: dict[str, Any]
    model: str | None = None
    duration_ms: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def tokens(self) -> dict[str, int]:
        u = self.usage or {}
        return {"input": int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0)),
                "output": int(u.get("output_tokens", 0)), "cache_read": int(u.get("cache_read_input_tokens", 0))}


class LLMBackend(Protocol):
    def complete(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        tools: Sequence[str] = (),
        model: str | None = None,
        effort: str | None = None,
    ) -> LLMResult: ...
