"""Invokes the Claude Code CLI in headless mode and parses its output.

Uses the locally authenticated `claude` CLI (subscription-based), not the
Anthropic API — no API key involved, no per-token billing.
"""

from __future__ import annotations

import json
import os
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


def git_ok(args: list[str], cwd: str) -> bool:
    """Like git(), but returns success/failure instead of raising or returning output."""
    proc = subprocess.run(["git", *args], cwd=cwd,
                          capture_output=True, text=True)
    return proc.returncode == 0


def ensure_worktree(repo: str, branch: str, base_branch: str, worktree_root: str) -> str:
    """Creates (or reuses) an isolated git worktree checked out to `branch`, so the
    implementer never commits directly onto `base_branch`."""
    path = os.path.join(worktree_root, branch.replace("/", "-"))
    if os.path.exists(os.path.join(path, ".git")):
        return path
    os.makedirs(worktree_root, exist_ok=True)
    # Clears out git's records of any worktree whose directory was removed
    # without `git worktree remove` (e.g. manually, or by a prior bug), which
    # would otherwise make git think `branch` is still checked out elsewhere
    # and permanently fail every future `worktree add` for it.
    git_ok(["worktree", "prune"], repo)
    if git_ok(["rev-parse", "--verify", branch], repo):
        git(["worktree", "add", path, branch], repo)
    else:
        git(["worktree", "add", "-b", branch, path, base_branch], repo)
    return path


def merge_branch(repo: str, branch: str, base_branch: str) -> None:
    """Merges an approved feature branch into base_branch in the main checkout.

    On failure (e.g. a conflict), aborts the in-progress merge so `repo` is
    left in a clean state instead of stuck mid-merge and blocking every
    subsequent card's merge attempt.
    """
    current = git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    if current != base_branch:
        git(["checkout", base_branch], repo)
    proc = subprocess.run(
        ["git", "merge", "--no-ff", "--no-edit", branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        git_ok(["merge", "--abort"], repo)
        raise RuntimeError(
            f"git merge --no-ff --no-edit {branch} into {base_branch} failed "
            f"(merge aborted): {proc.stderr}")


def remove_worktree(repo: str, path: str, branch: str) -> None:
    """Removes a feature worktree and its branch once it's been merged."""
    git(["worktree", "remove", path, "--force"], repo)
    git_ok(["branch", "-d", branch], repo)
