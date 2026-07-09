# agentloop

A simple agentic implement/review loop over a Trello kanban board, powered by a **Claude subscription** (the local `claude` CLI, headless) instead of the Anthropic API.

It's a scaled-down version of the pattern described in Bun's blog post on porting Bun from Zig to Rust (`bun.com/blog/bun-in-rust`): an implementer writes code, an adversarial reviewer (fresh context, diff-only) checks it, feedback gets applied, and the change is committed. Bun ran that with ~64 parallel API-billed agents; this runs it sequentially against a Trello board, at whatever pace a subscription's rate limits allow — no massive parallelism, no per-token billing.

## How it maps to your kanban board

```
To Do ──────────> In Progress ──────────> In Review ──────────> Done
                        ^                                          │
                        └──────────── changes requested ───────────┘
                                            │
                                (after too many review rounds)
                                            ▼
                                      Needs Human
```

- **To Do**: tasks waiting to be picked up. Card name + description become the task prompt.
- **In Progress**: the implementer (`claude`, headless) works here — reads the task, edits files, commits. It never touches your main checkout directly: each card gets its own git worktree on its own branch (see below).
- **In Review**: an adversarial reviewer (separate Claude invocation, sees *only* the diff) checks the change and either approves or requests changes.
- **Done**: reviewer approved — the card's branch has been merged into your base branch and its worktree cleaned up.
- **Needs Human**: the card bounced between In Progress/In Review too many times (`--max-review-rounds`, default 3) without approval, or an approved merge failed (e.g. conflict) — the loop stops touching it so a person can step in.

All state (which git commit a card corresponds to, how many review rounds happened) is stored as comments on the Trello card itself, not in a local file — so the script can be killed and restarted without losing track of anything.

## Isolated per-card git worktrees

Instead of committing straight to your main branch, every card gets its own `git worktree` checked out to a dedicated branch (`agentloop/<card-name-slug>-<card-id>`), created off `--base-branch` under `--worktree-dir`. The implementer only ever writes/commits inside that worktree, so in-progress work never touches or dirties your main branch — and several cards can be worked on without their changes stepping on each other.

When a reviewer approves a card, its branch is merged (`--no-ff`) back into `--base-branch` in your main checkout and the worktree + branch are deleted. If the merge fails (e.g. a conflict with another already-merged card), the card is routed to **Needs Human** instead of Done, and its worktree is left in place so you can resolve it by hand.

Branch/worktree naming is derived deterministically from the card itself (not from comment state), so restarting the script reattaches to the same in-progress branch automatically.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

```
TRELLO_API_KEY=...
TRELLO_TOKEN=...
TRELLO_BOARD_ID=...
```

List names default to `To Do` / `In Progress` / `In Review` / `Done` / `Needs Human` — override in `.env` if your board uses different names.

You'll also need the `claude` CLI installed and logged in with your subscription on the machine that runs this script (`claude` must work from the terminal already — this script doesn't handle login).

### Getting Trello credentials

The old `trello.com/app-key` page is retired. Current steps:

1. Go to `https://trello.com/power-ups/admin` and create (or open) a Power-Up — it doesn't need to be published, it's just a container for API credentials.
2. Open it → **API Key** tab → **Generate a new API Key**. That value is `TRELLO_API_KEY`.
3. Right next to the generated key there's a **Token** link — click it, click **Allow**, and the resulting value is `TRELLO_TOKEN`.
4. `TRELLO_BOARD_ID` is the ID segment in your board's URL: `trello.com/b/<BOARD_ID>/your-board-name`.

If you get `401 invalid key` from any request (even a basic `members/me` call), the credentials themselves are wrong or stale — regenerate them from the Power-Up admin page above.

## Usage

```bash
# Safe dry run: prints what it would do, doesn't call claude or mutate Trello
python agentloop.py --repo /path/to/target/repo --dry-run --once

# One real pass over the board
python agentloop.py --repo /path/to/target/repo --once

# Keep polling forever (default interval: 60s, override with --interval)
python agentloop.py --repo /path/to/target/repo
```

`--repo` is the local git checkout the reviewer reads from and where approved branches get merged. The implementer itself works in a per-card worktree derived from `--repo` (see above), never in `--repo`'s working tree directly.

### All flags

| Flag | Default | Meaning |
|---|---|---|
| `--repo` | *(required)* | path to the local git checkout to work on |
| `--once` | off | run a single pass over the board and exit |
| `--interval` | 60 | seconds to sleep between polling passes |
| `--concurrency` | 1 | max cards picked up from To Do per pass — keep low for a subscription plan |
| `--max-review-rounds` | 3 | after this many "request changes" rounds, move the card to Needs Human |
| `--dry-run` | off | don't call `claude` or mutate Trello, just log what would happen |
| `--base-branch` | `--repo`'s current branch | branch each card's worktree is created from and merged back into on approval |
| `--worktree-dir` | `.agentloop-worktrees` next to `--repo` | directory holding per-card git worktrees |

## Notes on the design

- The implementer runs with `--permission-mode bypassPermissions` (required for headless mode — there's no TTY to answer permission prompts) plus injected rules against destructive git commands (`git reset --hard`, `git stash`, `git add -A`, etc.). This is a prompt-level guardrail, not a hard technical one.
- The reviewer runs with `--permission-mode plan` (read-only) and is only ever given the diff text — never the implementer's reasoning — to keep the review adversarial rather than self-congratulatory.
