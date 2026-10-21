# Spiritboard: repository rules

These rules bind every coding agent (Claude Code, Codex, …) and every human working in this repo.
`AGENTS.md` is canonical; `CLAUDE.md` only contains `@AGENTS.md`. Before changing a file, read every `AGENTS.md`
from the repo root down to that file's directory. Closer files add to or override broader ones.

## 1. Start from latest main, in a dedicated worktree
- Every task that changes files starts from the latest `origin/main`, on its own branch, in its own git worktree.
  Never implement work directly in the primary checkout.
- All worktrees live in one sibling folder, `../spiritboard-worktrees/<slug>`. Never create worktrees inside the repo
  or scattered elsewhere.

  ```bash
  git fetch origin main
  git worktree add ../spiritboard-worktrees/<slug> -b <type>/<slug> origin/main
  ```

  `<type>` matches the conventional commit prefix (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`).
- If the branch or worktree name already exists, inspect it. Reuse it only when it is demonstrably the same task;
  otherwise choose a distinct slug. Never delete, reset, stash or absorb another task's work.
- Run all subsequent commands for the task inside its worktree.

## 2. Delegate heavily to subagents
- Use subagents proactively and by default whenever work has separable research, implementation, verification or
  review streams. Good scopes: codebase exploration, reference research, an isolated component, failure-mode tests,
  privacy review and an adversarial final review.
- The lead agent owns the plan, assigns each subagent an explicit outcome, allowed paths and verification
  responsibility, prevents overlapping file ownership, integrates the result and stays accountable for the final
  verification.
- On a shared branch only the lead stages and commits. Independently mergeable streams get their own child
  branch and worktree under `../spiritboard-worktrees/` rather than concurrent git operations on one branch.
- Keep tightly coupled edits with one owner. Substantial work gets at least one independent subagent review: give
  the reviewer the requirements and the diff, not the desired verdict. Resolve findings or record why they do not
  apply.

## 3. One change, one PR
- Each task is delivered as its own focused PR against `main`. Do not bundle unrelated changes.
- Before opening the PR, fetch `origin/main` and bring the branch up to date. Review the full diff and staged file
  list against `origin/main`.
- The owner authorizes agents to push completed branches and open PRs without separate approval.
- Do not merge a PR whose specific changes the owner has not reviewed or approved. Present the changes, validation
  evidence and open questions, then ask. A request to implement or open a PR is not approval to merge.
- After a PR merges, delete its remote branch, then remove only that exact worktree and local branch
  (`git worktree remove ../spiritboard-worktrees/<slug>`). Never use broad paths or globs.

## 4. Privacy and references
- Spiritboard reads local agent transcripts. Never commit real transcripts, session data, profile paths, account
  identifiers or anything from `.spiritboard/`. Tests and demos use synthetic data built in temporary directories.
- Upstream checkouts in `references/` may inform decisions; their code is never copied. Understand the idea and
  write the Spiritboard version independently.
- Secrets live in the environment. `.env*` files are git-ignored; only `.env.example` is committed.
- Anything that leaves the machine (posting, sending external API calls not already opted into) requires explicit
  approval per action.

## 5. Code
- Always use `uv` for Python. Never invoke `python`, `python3`, `pip` or `pip3` directly: `uv run …`, `uv add …`,
  `uv sync`. Python 3.13 locally (`.python-version`); the package supports 3.12+.
- `uv run ruff check .` and `uv run ruff format --check .` must pass (config in `pyproject.toml`). Never silence a
  rule inline without a one-line reason.
- No runtime package dependencies; dev tooling goes in the `dev` dependency group. Type hints everywhere; avoid `Any`.
- Prefer one simple current implementation. No backward-compatibility shims, speculative abstractions or generic
  frameworks without a concrete present need. Refactor all callers together and remove superseded code.
- Modules are named for what they are. No `utils.py`, `helpers.py`, `common.py`.
- Comment only what code can't show, ≤ 3 lines. Docs follow the code in the same change.
- For a bug: write the failing test first, then fix. Unit tests never hit the network or a real LLM.

## 6. Git
- Conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`), one line, lowercase subject.
- Small, focused commits. Run the lint, format and test commands below before each commit.

## Commands
```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
uv run spiritboard serve --demo --data-dir .spiritboard/demo --port 8766
```
