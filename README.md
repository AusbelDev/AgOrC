# Orchestrator

A local terminal app for coding tasks: a planner investigates your project, a worker implements the
plan, and a reviewer checks the resulting changes. Choose the provider, model, and reasoning level
independently for each role.

The first version uses **existing Codex and Gemini CLI logins**. Defaults are GPT-6.1 Sol with high
reasoning for planning/review, and Gemini 3.8 Flash with medium thinking for implementation. Model IDs
are editable; the backend and your account must support the selected model. The app does not silently
substitute another configured model.

## Start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a Linux or macOS terminal. Tested locally
against Codex CLI 0.162.1 and Gemini CLI 0.46.0's command surfaces.

```sh
uv sync
uv run orchestrator init
uv run orchestrator doctor
uv run orchestrator
```

Log in outside the app if needed:

```sh
codex login
gemini
```

Choose ChatGPT login in Codex and Sign in with Google in Gemini. The orchestrator leaves cached
credentials with the CLIs, does not copy them into run records, and removes API credential variables
from agent subprocess environments. Codex is explicitly configured for ChatGPT auth; Gemini gets
temporary settings enforcing Google OAuth. Expired logins fail the stage and can be retried after
logging in again. `doctor` checks local login indicators without making model requests.
Use `uv run orchestrator doctor --live` to verify each distinct role configuration with a minimal
model request. This uses your account's model quota.

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
backend = "gemini-cli"
auth = "login"
executable = "gemini"
models = ["gemini-3.8-flash"]
sandbox = false

[roles.planner]
provider = "my_codex"
model = "gpt-6.1-sol"
reasoning = "high"

[roles.worker]
provider = "my_google"
model = "gemini-3.8-flash"
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

Authentication still comes from the CLIs' normal credential stores. For separate accounts, launch
with the respective CLI-supported home environment variables pointing at **already authenticated**
stores; config profiles alone do not switch identities.

Codex reasoning is passed as `model_reasoning_effort`. Gemini thinking is set through temporary
`modelConfigs.overrides`. `default` preserves the backend's default effort. Available effort values
depend on the model; unsupported combinations fail visibly instead of falling back.

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
apply. Gemini gets a deny-by-default policy allowing repository reads and worker file edits; shell,
MCP, network tools, and nested agents are excluded. The orchestrator runs verification itself.
Gemini's optional `sandbox = true` uses its configured sandbox runtime, which must already be set up.
With `sandbox = false`, Gemini file access relies on its CLI workspace checks and tool policy, not OS
isolation. The generated task folder is trusted for this invocation via `--skip-trust`, without
changing saved folder trust settings. Existing system settings (plain JSON) and central admin policy
paths are preserved; incompatible enforced authentication fails instead of being overridden.
Central Gemini admin policies may further restrict these tools.

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
- `adapters.py`: backend protocol and registry, Codex/Gemini login implementations, offline fixture.
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
[Gemini headless mode](https://geminicli.com/docs/cli/headless/),
[Gemini policy engine](https://geminicli.com/docs/reference/policy-engine/),
[Gemini model configuration](https://geminicli.com/docs/cli/generation-settings/).

## Development

```sh
uv sync
uv run ruff check .
uv run pytest
```

The tests exercise the workflow on actual temporary Git repositories, failed-check gating, revisions,
interruption/recovery, provider stream parsing using subprocess fixtures, process termination, and
the TUI via Textual's headless pilot. They do not spend model quota or require credentials.
