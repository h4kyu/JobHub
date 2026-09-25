"""Headless Claude Code backend: shells out to `claude -p` so usage bills to the user's subscription.

Verified flags (claude CLI, 2026-08): --json-schema returns the object under `structured_output`;
--tools "" disables tools; --system-prompt replaces the default (saves ~5K tokens/call);
--no-session-persistence avoids writing session files.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Sequence

from .. import config
from .base import CLAUDE_DEFAULT, LLMError, LLMRateLimitError, LLMResult

_RATE_LIMIT_RE = re.compile(
    r"rate limit|usage limit|session limit|limit reached|limit exceeded|too many requests|429", re.I
)


class ClaudeCodeBackend:
    def __init__(
        self,
        model: str | None = None,
        effort: str | None = None,
        timeout_seconds: int = 900,
        claude_bin: str = "claude",
        max_retries: int = 1,
    ) -> None:
        self.model = model
        self.effort = effort
        self.timeout = timeout_seconds
        self.claude_bin = shutil.which(claude_bin) or claude_bin
        self.max_retries = max_retries

    def _cmd(self, system: str, schema: dict[str, Any], tools: Sequence[str], model: str | None, effort: str | None) -> list[str]:
        cmd = [
            self.claude_bin, "-p",
            "--no-session-persistence",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--system-prompt", system,
            "--tools", ",".join(tools) if tools else "",
        ]
        if tools:
            cmd += ["--allowedTools", ",".join(tools)]
        m = model if model is not None else self.model
        if m and m != CLAUDE_DEFAULT:
            cmd += ["--model", m]
        e = effort if effort is not None else self.effort
        if e and e != CLAUDE_DEFAULT:
            cmd += ["--effort", e]
        return cmd

    def complete(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        tools: Sequence[str] = (),
        model: str | None = None,
        effort: str | None = None,
    ) -> LLMResult:
        config.ensure_dirs()
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        cmd = self._cmd(system, schema, tools, model, effort)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            t0 = time.monotonic()
            try:
                proc = subprocess.run(
                    cmd, input=prompt, capture_output=True, text=True,
                    timeout=self.timeout, cwd=config.LLM_CWD, env=env,
                )
            except subprocess.TimeoutExpired as e:
                last_err = LLMError(f"claude -p timed out after {self.timeout}s")
                continue
            ms = int((time.monotonic() - t0) * 1000)
            try:
                out = json.loads(proc.stdout)
            except json.JSONDecodeError:
                msg = (proc.stderr or proc.stdout or "").strip()[:500]
                if _RATE_LIMIT_RE.search(msg):
                    raise LLMRateLimitError(msg)
                last_err = LLMError(f"non-JSON output from claude (rc={proc.returncode}): {msg}")
                continue
            result_text = str(out.get("result", ""))
            if out.get("is_error") or proc.returncode != 0:
                if _RATE_LIMIT_RE.search(result_text):
                    raise LLMRateLimitError(result_text)
                last_err = LLMError(f"claude error: {result_text[:500]}")
                continue
            data = out.get("structured_output")
            if data is None:
                # Fall back to parsing the text result as JSON.
                try:
                    data = json.loads(result_text)
                except json.JSONDecodeError:
                    last_err = LLMError(f"no structured_output in claude result: {result_text[:300]}")
                    continue
            used_model = None
            mu = out.get("modelUsage") or {}
            if mu:
                # Pick the model that did the real work (largest output tokens).
                used_model = max(mu.items(), key=lambda kv: kv[1].get("outputTokens", 0))[0]
            return LLMResult(
                data=data, model=used_model, duration_ms=int(out.get("duration_ms", ms)),
                usage=out.get("usage") or {}, session_id=out.get("session_id"), raw=out,
            )
        assert last_err is not None
        raise last_err


def backend_from_profile() -> ClaudeCodeBackend:
    llm = config.load_profile().llm
    return ClaudeCodeBackend(model=llm.model, effort=llm.effort, timeout_seconds=llm.timeout_seconds)
