# Orchestrator

A local terminal app for coding tasks: a planner investigates your project, a worker implements the
plan, and a reviewer checks the resulting changes. Choose the provider, model, and reasoning level
independently for each role.

The first version uses **existing Codex and Antigravity CLI logins**. Defaults are GPT-6.1 Sol with high
reasoning for planning/review, and Gemini 3.8 Flash with medium thinking for implementation. Model IDs
are editable; the backend and your account must support the selected model. The app does not silently
substitute another configured model.

## Start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a Linux or macOS terminal. Tested locally
against Codex CLI 0.162.1 and Antigravity CLI (`agy`) 1.3.3's command surfaces.

```sh
uv sync
uv run orchestrator init
uv run orchestrator doctor
uv run orchestrator
```

Log in outside the app if needed:

```sh
codex login
agy
```

Choose ChatGPT login in Codex and complete sign-in in `agy`. Antigravity's command is `agy`,
even though this app names its backend `agy-cli`. Its sign-in is separate from the retired Gemini
CLI's OAuth cache. Credentials remain in each CLI's normal stores; this app does not copy them into
run records and removes API credential variables from agent subprocess environments. Antigravity
uses its cached CLI account/keyring, without rewriting its settings or permissions. If its settings
select `modelProvider = "gemini"` for API authentication, remove that setting to use account login.

`doctor` checks CLI installation and Codex's login status. Antigravity has no equivalent login-status
subcommand, so a local check does not claim that its account is authenticated. Use
`uv run orchestrator doctor --live` to verify each distinct role configuration with a minimal model
request. This uses account quota. Expired logins fail visibly; run `agy` to sign in and then resume.

Try the **Offline demo** button to fix a small real bug in a generated sample project, run its tests,
and inspect the diff without contacting a provider. From the command line:

```sh
uv run orchestrator demo
```

## Configuration and profiles

`orchestrator init` creates `orchestrator.local.toml`. See
[orchestrator.example.toml](orchestrator.example.toml) for the full example. The TUI lets you choose
existing provider definitions, enter arbitrary model IDs, select reasoning, edit verification
commands, and save defaults. Role-specific instructions and provider definitions are configured in
TOML. Listed `models` provide input suggestions, not a restriction or online model catalog.

```toml
[providers.my_codex]
backend = "codex-cli"
auth = "login"
executable = "codex"
models = ["gpt-6.1-sol"]

[providers.my_google]
backend = "agy-cli"
auth = "login"
executable = "agy"
models = ["gemini-3.8-flash-medium"]
sandbox = false

[roles.planner]
provider = "my_codex"
model = "gpt-6.1-sol"
reasoning = "high"

[roles.worker]
provider = "my_google"
model = "gemini-3.8-flash-medium"
reasoning = "medium"

[roles.reviewer]
provider = "my_codex"
model = "gpt-6.1-sol"
reasoning = "high"

[workflow]
max_revision_rounds = 2
stage_timeout_seconds = 900
run_timeout_seconds = 3600
checks = [["python", "-m", "unittest", "discover", "-v"]]
```

Use separate configuration and state files for work and personal projects:

```sh
uv run orchestrator --config work.toml --state-dir ~/.local/state/orchestrator-work tui
```

Authentication comes from the CLIs' normal credential stores. Configuration profiles do not switch
identities, and this app does not automate account switching.

Codex reasoning is passed as `model_reasoning_effort`. Antigravity receives its native `--model`
slug and `--effort` flag. `default` omits the effort override. Model IDs are backend-specific:
`gemini-3.8-flash-medium` is the Antigravity slug for Gemini 3.8 Flash at medium reasoning.
Run `agy models` to list your account's available slugs, including any Claude models it exposes.
Unsupported model/effort combinations fail instead of falling back.

Old TOML profiles using `gemini-cli` are migrated in memory with a notice: provider names,
instructions, checks, and custom model IDs are retained; the normal `gemini` executable becomes
`agy`. The old default `gemini-3.8-flash` ID becomes the equivalent low/medium/high Antigravity slug.
Save settings in the TUI to persist the migration. Custom wrapper executables and unsupported
legacy effort levels require an explicit configuration update. Saved unfinished runs migrate when
resumed and record the change; historical reports retain their original model IDs.

## Workflow

1. Select a committed Git repository and describe the task, or paste a Jira issue's relevant text.
2. Create a dedicated `orchestrator/<run-id>` branch and worktree from the repository's current HEAD.
   Existing uncommitted changes and ignored files are not copied into the worktree.
3. Ask the planner for a structured plan and acceptance criteria.
4. Ask the worker to implement it. Each invocation gets the saved plan, review, check results, and
   additional user instructions.
5. Execute your configured checks in that worktree, using argv arrays without a shell.
6. Start a fresh reviewer invocation with the plan, diff, source access, and check evidence.
7. Revise until accepted with passing checks, or until the revision/time limit is reached.

`max_revision_rounds = 2` allows an initial implementation plus two revisions. A reviewer cannot
override a failed check. If checks are empty, the report explicitly states that test success has not
been established; completion means the review was accepted. Set project-appropriate verification
commands before using it on real tasks. Checks run on the host with your normal permissions, and
should be commands you trust. Worktrees do not include another checkout's virtualenv or dependencies.

The app creates changes for inspection. It does not commit those changes, merge, push, create PRs,
or write to Jira. The generated worktrees and artifacts are retained. Inspect/copy changes using Git
or your editor; clean up a worktree yourself when you no longer need it.

**Execution policy:** Codex uses its read-only sandbox for planner/reviewer and workspace-write
sandbox for worker, with interactive escalation disabled. This version runs it without the user's
global Codex configuration to enforce the selected model/auth/sandbox; repository instructions still
apply. Antigravity uses its native `--mode plan` for planner/reviewer and `--mode accept-edits`
for worker, and preserves your existing permission rules. Plan mode requests analysis before edits;
it is not an OS read-only sandbox. The engine detects tracked/untracked source changes made by the
planner/reviewer and fails the stage for inspection. Existing Antigravity command, MCP, and web
grants still apply; headless requests requiring interactive approval are soft-denied by the CLI.
The adapter does not use `--dangerously-skip-permissions` and does not install global permission rules.
The orchestrator runs configured checks itself. `sandbox = true` adds Antigravity's native terminal
sandbox flag; it restricts terminal execution and does not guarantee read-only file tools.

## Pause, stop, resume, and history

- **Pause** (`Ctrl+P`): finish the current stage and pause at the next boundary.
- **Stop** (`Ctrl+X`): interrupt the active agent/check process group; keep partial edits.
- **Resume** (`Ctrl+R`): retry the pending stage using its saved configuration and existing worktree.
- **Send**: add instructions for the next stage. Instructions added while review is running require
  another worker/review pass before completion.
- **History**: select a previous run to inspect activity, plan, review, and the saved diff.
- **Quit** (`Ctrl+Q`): stop active execution before exiting.

Resume is workflow-level recovery using saved artifacts and fresh agent invocations, rather than
reopening native provider conversations. The app validates branch and HEAD; worktree edits detected
before a pending review invalidate prior check results. An interrupted worker first inspects its
partial changes. Runs are locked against concurrent execution. Saved snapshots apply to resumed
runs; changing defaults affects new runs.

```sh
uv run orchestrator run "Fix the empty-input bug" --repo /path/to/project --check "python -m unittest"
uv run orchestrator history
uv run orchestrator show RUN_ID
uv run orchestrator resume RUN_ID --note "Preserve the existing public API"
```

By default, SQLite history, events, worktrees, and artifacts live in
`$XDG_STATE_HOME/orchestrator` or `~/.local/state/orchestrator`. Each run has `report.md`,
`changes.diff`, prompts, responses, and check results. Source/task text appears in these local records;
choose the state directory appropriate to your project. Output excerpts are bounded for memory and
context size; the reviewer can inspect files directly in the worktree.

## Architecture and next adapters

- `config.py`: named providers, roles, validation, TOML profiles.
- `adapters.py`: backend protocol and registry, Codex/Antigravity login implementations, offline fixture.
- `processes.py`: async streaming, output limits, timeouts, process-group cancellation.
- `git.py`: worktrees, diffs including untracked files, workspace fingerprints.
- `engine.py`: stages, structured contracts, checks, revisions, interruption/recovery.
- `store.py`: SQLite records, event history, artifacts, execution locks.
- `tui.py` / `cli.py`: terminal interface and headless commands.

Future Claude Code login and direct API adapters implement `AgentBackend.run(AgentRequest, emit)` and
return an `AgentResult`. Add their configuration/capability validation and register the backend; the
workflow remains independent of provider SDKs. API adapters will also need their own scoped execution
tool loop. Claude, direct API auth, automatic Jira import, native model discovery, and concurrent
workers are **not implemented** in this version.

Integration references:
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[Antigravity headless mode](https://www.antigravity.google/docs/cli/headless/),
[Antigravity execution modes](https://www.antigravity.google/docs/cli/modes/),
[Antigravity permissions](https://www.antigravity.google/docs/permissions/).

Google documents headless mode for direct scripting and CI, and says wrapping it in third-party
applications is unsupported under consumer plans. See the
[Antigravity FAQ](https://www.antigravity.google/docs/faq/#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching).
This adapter invokes the native CLI; it does not extract login tokens or implement account rotation.

## Development

```sh
uv sync
uv run ruff check .
uv run pytest
```

The tests exercise the workflow on actual temporary Git repositories, failed-check gating, revisions,
interruption/recovery, provider stream parsing using subprocess fixtures, process termination, and
the TUI via Textual's headless pilot. They do not spend model quota or require credentials.
