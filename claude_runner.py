"""Invokes the Claude Code CLI in headless mode and parses its output.

Uses the locally authenticated `claude` CLI (subscription-based), not the
Anthropic API — no API key involved, no per-token billing.
"""

from __future__ import annotations

import json
import re
import subprocess

JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


class ClaudeRunError(RuntimeError):
    pass


def run_claude(
    prompt: str,
    cwd: str,
    permission_mode: str = "acceptEdits",
    append_system_prompt: str | None = None,
    timeout: int = 1800,
) -> dict:
    """Runs `claude -p <prompt>` headless in `cwd` and returns the parsed JSON result."""
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-mode",
        permission_mode,
    ]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]

    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise ClaudeRunError(
            f"claude exited {proc.returncode}: {proc.stderr[-2000:]}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeRunError(
            f"could not parse claude output as JSON: {exc}\n{proc.stdout[-2000:]}") from exc


def extract_json_block(result_text: str) -> dict | None:
    """Pulls the last ```json ... ``` fenced block out of a model response, if any."""
    matches = JSON_BLOCK_RE.findall(result_text)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return None


def git(args: list[str], cwd: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()
