#!/usr/bin/env python3
"""Simple agentic loop over a Trello kanban board, powered by a Claude
subscription (Claude Code CLI, headless) instead of massive API parallelism.

Kanban mapping:
    To Do       -> picked up and moved to In Progress
    In Progress -> implementer works here (writes code, commits)
    In Review   -> adversarial reviewer works here (reads the diff only)
    Done        -> reviewer approved
    Needs Human -> too many review rounds without approval, stops looping
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

from dotenv import load_dotenv

from claude_runner import (
    ClaudeRunError,
    ensure_worktree,
    extract_json_block,
    git,
    merge_branch,
    remove_worktree,
    run_claude,
)
from trello_client import TrelloClient

SHA_MARKER_RE = re.compile(r"\[agentloop\] before=(\S+) after=(\S+)")
CLEANUP_ATTEMPTS_RE = re.compile(r"attempts=(\d+)")

# After this many failed retries, stop retrying and leave a permanent
# comment instead of retrying silently forever.
MAX_CLEANUP_ATTEMPTS = 5

IMPLEMENTER_RULES = (
    "You are operating unattended in headless mode as part of an automated "
    "kanban pipeline. Rules:\n"
    "- Never run `git reset --hard`, `git stash`, or `git clean -f`.\n"
    "- Stage only the specific files you changed (`git add <file>`), never "
    "`git add -A` or `git add .`.\n"
    "- Make exactly one commit for this task before finishing.\n"
    "- Do not leave stub functions or TODO placeholders — implement the task "
    "fully.\n"
    "- Do not write long comments justifying a workaround; fix the root "
    "cause instead."
)

REVIEWER_RULES = (
    "You are an adversarial code reviewer. Assume the diff below is wrong "
    "until proven otherwise. Look for concrete bugs: logic errors, edge "
    "cases, resource/memory issues, incorrect assumptions. Reject any "
    "comment in the diff that merely justifies a workaround instead of "
    "fixing the root cause. You are read-only — do not modify any files.\n\n"
    "End your response with exactly one fenced json block:\n"
    '```json\n{"verdict": "approve" or "request_changes", "summary": '
    '"...", "issues": ["..."]}\n```'
)


REQUIRED_DOCS = ("CONVENTIONS.md", "CONTEXT.md")


def log(msg: str) -> None:
    print(f"[agentloop] {msg}", flush=True)


def missing_docs(repo: str) -> list[str]:
    docs_dir = os.path.join(repo, "docs")
    return [name for name in REQUIRED_DOCS
            if not os.path.isfile(os.path.join(docs_dir, name))]


def check_repo_or_exit(repo: str) -> None:
    if not os.path.isdir(repo):
        sys.exit(f"--repo path does not exist or is not a directory: {repo}")
    if not os.path.exists(os.path.join(repo, ".git")):
        sys.exit(f"--repo is not a git repository (no .git found): {repo}")


def check_docs_or_confirm(repo: str, assume_yes: bool, dry_run: bool) -> None:
    missing = missing_docs(repo)
    if not missing:
        return

    log(
        f"warning: {repo} is missing docs/{' and docs/'.join(missing)}. "
        "Without project conventions/context, the implementer is more likely "
        "to hallucinate or diverge from how this codebase actually works."
    )

    if assume_yes:
        log("--yes passed, continuing despite missing docs")
        return

    if dry_run:
        log("--dry-run passed, continuing without prompting (dry runs never mutate state)")
        return

    if not sys.stdin.isatty():
        raise SystemExit(
            "Refusing to continue without docs/CONVENTIONS.md and docs/CONTEXT.md "
            "in a non-interactive session. Add the missing file(s) or re-run with "
            "--yes to proceed anyway."
        )

    try:
        answer = input("Continue anyway? [y/N] ").strip().lower()
    except EOFError:
        raise SystemExit(
            "Aborted: stdin closed before confirmation could be read"
        )
    if answer not in ("y", "yes"):
        raise SystemExit("Aborted: missing docs/CONVENTIONS.md and/or docs/CONTEXT.md")


def resolve_list_ids(client: TrelloClient, names: dict[str, str]) -> dict[str, str | None]:
    resolved = {}
    for key, name in names.items():
        list_id = client.list_id_by_name(name)
        if list_id is None and key != "needs_human":
            raise SystemExit(
                f"Could not find Trello list named '{name}' on the board")
        resolved[key] = list_id
    return resolved


def latest_request_changes_comment(comments: list[str]) -> str | None:
    for text in comments:
        if text.startswith("[review] REQUEST_CHANGES"):
            return text
    return None


def count_request_changes(comments: list[str]) -> int:
    return sum(1 for text in comments if text.startswith("[review] REQUEST_CHANGES"))


def parse_last_shas(comments: list[str]) -> tuple[str, str] | tuple[None, None]:
    for text in comments:
        match = SHA_MARKER_RE.search(text)
        if match:
            return match.group(1), match.group(2)
    return None, None


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40].strip("-") or "task"


def branch_name_for_card(card: dict) -> str:
    return f"agentloop/{slugify(card['name'])}-{card['id'][:8]}"


def worktree_path_for(worktree_root: str, branch: str) -> str:
    return os.path.join(worktree_root, branch.replace("/", "-"))


def build_implement_prompt(card: dict, feedback: str | None) -> str:
    prompt = f"Task from Trello card '{card['name']}':\n\n{card.get('desc') or '(no description)'}"
    if feedback:
        prompt += f"\n\nA previous review requested changes:\n{feedback}\n\nAddress this feedback."
    return prompt


def build_review_prompt(card: dict, diff: str) -> str:
    return (
        f"You are reviewing a code change for the Trello task '{card['name']}'.\n\n"
        f"Task description:\n{card.get('desc') or '(no description)'}\n\n"
        f"Diff to review:\n```diff\n{diff}\n```\n\n{REVIEWER_RULES}"
    )


def implement(
    client: TrelloClient,
    card: dict,
    repo: str,
    list_ids: dict,
    dry_run: bool,
    base_branch: str,
    worktree_root: str,
) -> None:
    comments = client.comments(card["id"])
    feedback = latest_request_changes_comment(comments)
    prompt = build_implement_prompt(card, feedback)
    branch = branch_name_for_card(card)

    if dry_run:
        log(
            f"[dry-run] would implement '{card['name']}' on branch '{branch}' "
            f"with prompt:\n{prompt}"
        )
        return

    log(f"Implementing '{card['name']}' on branch '{branch}'")
    worktree = ensure_worktree(repo, branch, base_branch, worktree_root)
    before_sha = git(["rev-parse", "HEAD"], worktree)
    try:
        run_claude(
            prompt,
            cwd=worktree,
            permission_mode="bypassPermissions",
            append_system_prompt=IMPLEMENTER_RULES,
        )
    except ClaudeRunError as exc:
        log(f"Implementer failed on '{card['name']}': {exc}")
        client.add_comment(card["id"], f"[agentloop] implementer error: {exc}")
        return
    after_sha = git(["rev-parse", "HEAD"], worktree)

    client.add_comment(
        card["id"],
        f"[agentloop] before={before_sha} after={after_sha} branch={branch}",
    )
    client.move_card(card["id"], list_ids["in_review"])


def review(
    client: TrelloClient,
    card: dict,
    repo: str,
    list_ids: dict,
    max_review_rounds: int,
    dry_run: bool,
    base_branch: str,
    worktree_root: str,
    pending_cleanup: dict,
) -> None:
    comments = client.comments(card["id"])
    before_sha, after_sha = parse_last_shas(comments)
    if before_sha is None:
        log(f"'{card['name']}' is in In Review with no [agentloop] marker — skipping")
        return

    rounds_so_far = count_request_changes(comments)
    if rounds_so_far >= max_review_rounds:
        log(f"'{card['name']}' hit max review rounds ({max_review_rounds})")
        if not dry_run and list_ids.get("needs_human"):
            client.add_comment(
                card["id"],
                f"[agentloop] stuck after {rounds_so_far} review rounds, needs a human",
            )
            client.move_card(card["id"], list_ids["needs_human"])
        return

    diff = git(["diff", f"{before_sha}..{after_sha}"], repo)
    prompt = build_review_prompt(card, diff)

    if dry_run:
        log(f"[dry-run] would review '{card['name']}' with diff of {len(diff)} chars")
        return

    branch = branch_name_for_card(card)
    worktree = worktree_path_for(worktree_root, branch)
    review_cwd = worktree if os.path.isdir(worktree) else repo
    if review_cwd is repo:
        log(
            f"worktree for '{card['name']}' not found at {worktree}, "
            "reviewer will fall back to reading --repo (may be stale)"
        )

    log(f"Reviewing '{card['name']}'")
    try:
        result = run_claude(prompt, cwd=review_cwd, permission_mode="plan")
    except ClaudeRunError as exc:
        log(f"Reviewer failed on '{card['name']}': {exc}")
        return

    verdict = extract_json_block(result.get("result", ""))
    if verdict is None:
        log(
            f"Could not parse a verdict for '{card['name']}', leaving it in In Review")
        return

    if verdict.get("verdict") == "approve":
        try:
            merge_branch(repo, branch, base_branch)
        except RuntimeError as exc:
            log(f"Could not merge branch '{branch}' for '{card['name']}': {exc}")
            client.add_comment(
                card["id"],
                f"[agentloop] approved but merge into {base_branch} failed, needs a human: {exc}",
            )
            if list_ids.get("needs_human"):
                client.move_card(card["id"], list_ids["needs_human"])
            return

        client.add_comment(
            card["id"], f"[review] APPROVE: {verdict.get('summary', '')}")
        client.move_card(card["id"], list_ids["done"])

        try:
            remove_worktree(repo, worktree, branch)
        except RuntimeError as exc:
            log(f"Merged '{branch}' for '{card['name']}' but cleanup failed: {exc}")
            client.add_comment(
                card["id"],
                f"[agentloop] cleanup pending: branch={branch} attempts=1 error={exc}",
            )
            pending_cleanup[card["id"]] = {"card": card, "attempt": 1}
    else:
        issues = "\n".join(f"- {issue}" for issue in verdict.get("issues", []))
        client.add_comment(
            card["id"],
            f"[review] REQUEST_CHANGES: {verdict.get('summary', '')}\n{issues}",
        )
        client.move_card(card["id"], list_ids["in_progress"])


def discover_pending_cleanups(client: TrelloClient, done_list_id: str) -> dict[str, dict]:
    """One-time scan of Done for cards whose cleanup is still pending, so a
    restarted process reattaches to them. This is intentionally called once
    at startup rather than every poll pass — scanning comments for every
    card in Done on every pass would grow unbounded over the life of a
    long-running loop."""
    pending: dict[str, dict] = {}
    for card in client.cards_in_list(done_list_id):
        comments = client.comments(card["id"])
        if any(
            text.startswith("[agentloop] cleanup done")
            or text.startswith("[agentloop] cleanup abandoned")
            for text in comments
        ):
            continue
        marker = next(
            (text for text in comments if text.startswith("[agentloop] cleanup pending")),
            None,
        )
        if marker is None:
            continue
        match = CLEANUP_ATTEMPTS_RE.search(marker)
        attempt = int(match.group(1)) if match else 1
        pending[card["id"]] = {"card": card, "attempt": attempt}
    return pending


def retry_pending_cleanup(
    client: TrelloClient, card: dict, repo: str, worktree_root: str, attempt: int
) -> int | None:
    """Retries worktree/branch removal for a merged card whose cleanup failed
    earlier. Branch and worktree are recomputed deterministically from the
    card rather than parsed out of the comment text, so a worktree path
    containing spaces (e.g. under a directory named "My Documents") can't
    break parsing. Returns the next attempt count if cleanup is still
    pending, or None once it's resolved (cleaned up, or abandoned after
    MAX_CLEANUP_ATTEMPTS)."""
    branch = branch_name_for_card(card)
    worktree = worktree_path_for(worktree_root, branch)
    try:
        remove_worktree(repo, worktree, branch)
    except RuntimeError as exc:
        if attempt >= MAX_CLEANUP_ATTEMPTS:
            log(
                f"Cleanup for '{card['name']}' failed {attempt} times, giving up: {exc}"
            )
            client.add_comment(
                card["id"],
                f"[agentloop] cleanup abandoned: branch={branch} worktree={worktree} "
                f"failed after {attempt} attempts, remove manually: {exc}",
            )
            return None
        next_attempt = attempt + 1
        log(f"Retry cleanup for '{card['name']}' still failing (attempt {attempt}): {exc}")
        client.add_comment(
            card["id"],
            f"[agentloop] cleanup pending: branch={branch} attempts={next_attempt} error={exc}",
        )
        return next_attempt

    log(f"Cleaned up branch '{branch}' for '{card['name']}' on retry")
    client.add_comment(card["id"], f"[agentloop] cleanup done: branch={branch}")
    return None


def run_loop(
    client: TrelloClient,
    repo: str,
    list_ids: dict,
    args: argparse.Namespace,
    base_branch: str,
    worktree_root: str,
) -> None:
    pending_cleanup: dict[str, dict] = (
        {} if args.dry_run else discover_pending_cleanups(client, list_ids["done"])
    )

    while True:
        todo_cards = client.cards_in_list(list_ids["todo"])
        for card in todo_cards[: args.concurrency]:
            log(f"Picking up '{card['name']}' from To Do")
            if not args.dry_run:
                client.move_card(card["id"], list_ids["in_progress"])
            implement(client, card, repo, list_ids,
                      args.dry_run, base_branch, worktree_root)

        for card in client.cards_in_list(list_ids["in_progress"]):
            implement(client, card, repo, list_ids,
                      args.dry_run, base_branch, worktree_root)

        for card in client.cards_in_list(list_ids["in_review"]):
            review(client, card, repo, list_ids, args.max_review_rounds,
                   args.dry_run, base_branch, worktree_root, pending_cleanup)

        if not args.dry_run:
            for card_id in list(pending_cleanup.keys()):
                entry = pending_cleanup[card_id]
                next_attempt = retry_pending_cleanup(
                    client, entry["card"], repo, worktree_root, entry["attempt"])
                if next_attempt is None:
                    del pending_cleanup[card_id]
                else:
                    entry["attempt"] = next_attempt

        if args.once:
            break
        time.sleep(args.interval)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True,
                        help="path to the local git checkout to work on")
    parser.add_argument("--once", action="store_true",
                        help="run a single pass and exit")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("POLL_INTERVAL_SECONDS", "60")),
        help="seconds to sleep between polling passes",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="max cards to pick up from To Do per pass (keep low for a subscription plan)",
    )
    parser.add_argument(
        "--max-review-rounds",
        type=int,
        default=3,
        help="after this many REQUEST_CHANGES rounds, move the card to Needs Human",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="don't call claude or mutate Trello, just print what would happen",
    )
    parser.add_argument(
        "--base-branch",
        default=os.environ.get("BASE_BRANCH"),
        help="branch each card's feature worktree is created from and merged back "
             "into on approval (default: --repo's current branch)",
    )
    parser.add_argument(
        "--worktree-dir",
        default=os.environ.get("WORKTREE_DIR"),
        help="directory to hold per-card git worktrees "
             "(default: a .agentloop-worktrees folder next to --repo)",
    )
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="skip the confirmation prompt when --repo is missing "
             "docs/CONVENTIONS.md or docs/CONTEXT.md",
    )
    args = parser.parse_args()

    check_repo_or_exit(args.repo)
    check_docs_or_confirm(args.repo, args.yes, args.dry_run)

    api_key = os.environ.get("TRELLO_API_KEY")
    token = os.environ.get("TRELLO_TOKEN")
    board_id = os.environ.get("TRELLO_BOARD_ID")
    if not (api_key and token and board_id):
        sys.exit(
            "TRELLO_API_KEY, TRELLO_TOKEN and TRELLO_BOARD_ID must be set (see .env.example)")

    client = TrelloClient(api_key, token, board_id)
    list_ids = resolve_list_ids(
        client,
        {
            "todo": os.environ.get("TODO_LIST_NAME", "To Do"),
            "in_progress": os.environ.get("IN_PROGRESS_LIST_NAME", "In Progress"),
            "in_review": os.environ.get("IN_REVIEW_LIST_NAME", "In Review"),
            "done": os.environ.get("DONE_LIST_NAME", "Done"),
            "needs_human": os.environ.get("NEEDS_HUMAN_LIST_NAME", "Needs Human"),
        },
    )

    base_branch = args.base_branch or git(
        ["rev-parse", "--abbrev-ref", "HEAD"], args.repo)
    worktree_root = args.worktree_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.repo)), ".agentloop-worktrees")

    run_loop(client, args.repo, list_ids, args, base_branch, worktree_root)


if __name__ == "__main__":
    main()
