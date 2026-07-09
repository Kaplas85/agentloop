# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`agentloop` is a small Python script that runs an agentic implement/review loop over a Trello kanban board, using a **Claude subscription** (the local `claude` CLI, headless) instead of the Anthropic API. It is a deliberately scaled-down version of the pattern described in Bun's blog post about porting Bun from Zig to Rust (`bun.com/blog/bun-in-rust`): implementer writes code → adversarial reviewer (separate context) checks the diff → feedback is applied → commit. Bun ran that pattern with ~64 parallel API-billed agents; this repo runs it sequentially (configurable concurrency, default 1) against whatever a subscription's rate limits allow — no massive parallelism, no per-token billing.

Trello itself doubles as both the task queue and the durable state store: card position in the board's lists IS the state machine, and card comments carry data the script needs to resume (git SHAs, review verdicts) across runs — there is no local database.

## Commands

```bash
pip install -r requirements.txt          # requests, python-dotenv

cp .env.example .env                      # then fill in TRELLO_API_KEY / TRELLO_TOKEN / TRELLO_BOARD_ID

python agentloop.py --repo /path/to/target/repo --dry-run --once   # safe smoke test, no mutations
python agentloop.py --repo /path/to/target/repo --once             # one real pass
python agentloop.py --repo /path/to/target/repo                    # continuous polling loop
```

There is no test suite or linter configured yet. `python3 -m py_compile agentloop.py trello_client.py claude_runner.py` is the fastest correctness check available; `--dry-run` is the way to validate loop/prompt logic against a real board without invoking `claude` or mutating Trello.

## Architecture

Three files, each with one job:

- **`agentloop.py`** — the orchestrator and CLI entrypoint. Owns the kanban state machine, prompt construction, and the polling loop (`run_loop`).
- **`trello_client.py`** — thin `requests`-based wrapper over the Trello REST API (`api.trello.com/1`, key+token as query params). No business logic here.
- **`claude_runner.py`** — shells out to the local `claude` CLI (`claude -p ... --output-format json`) and to `git`. This is what makes the loop subscription-based rather than API-based: it never touches an Anthropic API key.

### Kanban state machine

Lists on the Trello board ARE the states (names configurable via env vars, resolved to list IDs at startup in `resolve_list_ids`):

```
To Do ──implement()──> In Progress ──implement()──> In Review ──review()──> Done
                             ^                            │
                             └────── request_changes ─────┘
                                            │
                                  (after --max-review-rounds)
                                            ▼
                                      Needs Human
```

`run_loop` scans each list every pass: cards in **To Do** get moved to **In Progress** and immediately implemented; cards found sitting in **In Progress** (i.e., bounced back by a reviewer) get re-implemented with the reviewer's feedback folded into the prompt; cards in **In Review** get reviewed. A card only stays in a list between passes if it's waiting on the next stage.

### Why state lives in Trello comments, not a local file

After `implement()` runs, it records `before`/`after` git SHAs as a card comment: `[agentloop] before=<sha> after=<sha>`. `review()` reads that comment back to compute the diff. Review verdicts are likewise posted as `[review] APPROVE: ...` / `[review] REQUEST_CHANGES: ...` comments, and `count_request_changes` / `latest_request_changes_comment` parse the comment history to know how many rounds have happened and what feedback to feed back into the next `implement()` call. This makes the script fully stateless and restart-safe — killing and re-running it just re-derives state from the board.

### Adversarial review is diff-only by design

`review()` computes the diff itself in Python (`git diff before..after`) and hands only that text to the reviewer prompt — the reviewer never sees the implementer's reasoning or gets to run its own `git diff`. This mirrors the bias-avoidance argument from the Bun post ("the Claude that wrote the code wants it accepted; the Claude that reviews wants to find issues") and is why the reviewer is invoked with `--permission-mode plan` (read-only) while the implementer runs with `--permission-mode bypassPermissions` (needed since headless mode has no TTY to answer permission prompts).

Guardrails against destructive git operations (`git reset --hard`, `git stash`, `git clean -f`, `git add -A`) are enforced only via `IMPLEMENTER_RULES` injected through `--append-system-prompt` — this is a soft, prompt-level guardrail, not a hard technical one. A `PreToolUse` hook in the target repo's `.claude/settings.json` would be a stronger enforcement if this becomes an issue in practice.

### Getting Trello credentials

The old `trello.com/app-key` page is deprecated. Current flow:
1. Create (or reuse) a Power-Up at `https://trello.com/power-ups/admin`.
2. Open it → "API Key" tab → "Generate a new API Key" → this is `TRELLO_API_KEY`.
3. Next to the generated key there's a "Token" link → click it → Allow → the resulting token is `TRELLO_TOKEN`.
4. `TRELLO_BOARD_ID` is the ID in the board's URL (`trello.com/b/<BOARD_ID>/...`) or from `GET /1/members/me/boards`.

A `401 invalid key` error from any endpoint (even `/1/members/me`) means the credentials themselves are wrong/stale — regenerate them, it isn't a code issue.
