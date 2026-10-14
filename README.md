# Orchestrator

A local terminal app for coding tasks: a planner investigates your project, a worker implements the
plan, and a reviewer checks the resulting changes. Choose the provider, model, and reasoning level
independently for each role.

The app uses **existing Codex, Antigravity, and Claude Code CLI credentials**. Codex supports either
ChatGPT login or a cached OpenAI API key; Antigravity and Claude use account login. Defaults are
GPT-6.1 Sol with high reasoning for planning/review, and Gemini 3.8 Flash with medium thinking for
implementation.
Model IDs are editable; the backend and your account must support the selected model. The app does not silently
substitute another configured model.

## Quick start

You need Python 3.9+, [uv](https://docs.astral.sh/uv/), a Linux or macOS terminal, and a target Git
repository with at least one commit. Install the CLIs for your chosen providers and make their
commands available on `PATH`: `codex`, `agy`, and/or `claude`. The default roles use Codex and
Antigravity; Claude Code is optional.

1. **Set up the app.** From your Orchestrator checkout, install dependencies and create the profile:

   ```sh
   cd /path/to/orchestrator
   uv sync
   uv run orchestrator init
   ```

   `init` creates `orchestrator.local.toml`; run it once. If that file already exists, keep it and
   skip `init`. Provider definitions and additional role instructions can be edited in this file.

2. **Sign in to the providers you will use.** Run the relevant commands in your normal terminal;
   existing logins can be reused:

   ```sh
   codex login            # Choose ChatGPT login.
   agy                    # Complete Antigravity sign-in.
   claude auth login      # Only needed for Claude Code roles.
   ```

3. **Open the TUI for your project.** Continue running from the Orchestrator checkout:

   ```sh
   uv run orchestrator tui --repo /absolute/path/to/your/project
   ```

   `uv run orchestrator` also opens the TUI, with the current directory as the initial repository.
   Try **Offline demo** first to see a complete bug fix and its checks without using model quota.

4. **Configure your task.** Confirm **Repository**, describe the bug or change under **What should
   be changed?**, and choose each role's provider, model ID, and reasoning level under **MODELS**.
   Scroll the left panel to **VERIFICATION** and enter your project's checks as JSON argv arrays,
   for example `[ ["uv", "run", "pytest"] ]` for a Python project using uv. These commands run in
   the task worktree. In **Skills**, click **Refresh skill catalog** and enter any required work
   skills by name. Click **Save settings** to reuse your choices for future runs.

   You can quit with `Ctrl+Q` and check the saved provider setup before starting a real task:

   ```sh
   uv run orchestrator doctor
   uv run orchestrator doctor --live   # Optional: uses account quota or API usage.
   uv run orchestrator tui --repo /absolute/path/to/your/project
   ```

5. **Run and inspect the result.** Click **Start task**. Watch **Activity** and the workflow
   indicator; inspect **Plan**, **Review**, and **Diff** as the run progresses. **Pause** (`Ctrl+P`)
   waits for the current stage to finish, **Stop** (`Ctrl+X`) interrupts it, and **Resume** (`Ctrl+R`)
   retries the pending stage. Use the instruction field and **Send** to steer the next stage.
   Open **History** to revisit saved runs. The run overview shows the generated worktree path:
   inspect and test the changes there before committing or merging them yourself. Your original
   checkout keeps its existing changes; uncommitted changes are not copied into the task worktree.

Tested locally against Codex CLI 0.162.1 and Antigravity CLI (`agy`) 1.3.3's command surfaces.

Choose ChatGPT login in Codex, or configure [Codex API-key authentication](#codex-api-key-authentication),
and complete sign-in in `agy`. Antigravity's command is `agy`,
even though this app names its backend `agy-cli`. Its sign-in is separate from the retired Gemini
CLI's OAuth cache. Credentials remain in each CLI's normal stores; this app does not copy them into
run records and removes API credential variables from agent subprocess environments. Antigravity
uses its cached CLI account/keyring, without rewriting its settings or permissions. If its settings
select `modelProvider = "gemini"` for API authentication, remove that setting to use account login.

`doctor` checks the CLIs used by your current roles and Codex/Claude Code authentication status,
including whether Codex's login matches your selected authentication method. Unused providers
do not need to be installed. Antigravity has no equivalent login-status
subcommand, so a local check does not claim that its account is authenticated. Use
`uv run orchestrator doctor --live` to verify each distinct role configuration with a minimal model
request. This uses account quota or API usage, depending on the provider's authentication. Expired
logins fail visibly; run `agy` to sign in and then resume.

The **Offline demo** fixes a small real bug in a generated sample project, runs its tests, and shows
the diff without contacting a provider. You can also run it from the command line:

```sh
uv run orchestrator demo
```

## Codex API-key authentication

Codex roles can use your existing OpenAI API-key login in the Codex CLI. Run the following as the
same user who runs the TUI, inside the container and without `sudo`:

```sh
codex login status
```

If it already reports an API-key login, keep that login. Otherwise, configure the key using Codex's
native command. When your container supplies the key through `OPENAI_API_KEY`, pipe it to stdin:

```sh
printenv OPENAI_API_KEY | codex login --with-api-key
codex login status
```

`OPENAI_API_KEY` must contain your actual key before running this command. Codex stores the login in
its credential store; do not put the key in `orchestrator.local.toml` or the task description.
Orchestrator removes API credential environment variables from agent subprocesses and reuses the
cached Codex login. Simply exporting a key without configuring the CLI login is not sufficient.
API-key requests use your OpenAI Platform account rather than ChatGPT plan credits; see
[official OpenAI authentication documentation](https://learn.chatgpt.com/docs/auth).

In the TUI, scroll the left panel below **MODELS** to **AUTHENTICATION**. Change **codex · Codex
authentication** from **ChatGPT login** to **API key**, then click **Save settings**. This setting
applies to every role using that named provider. Custom Codex provider definitions have their own
selector. There is no key-entry field; credentials stay with the CLI.

The equivalent change in your existing provider section is:

```toml
[providers.codex]
backend = "codex-cli"
auth = "api"
executable = "codex"
models = ["gpt-6.1-sol"]
```

Keep the model IDs appropriate to your account; model availability may differ between authentication
methods. API authentication works with the same per-role MCP settings described under
[Jira MCP access](#jira-mcp-access). Run `uv run orchestrator doctor` to check the configured login
before starting a new task. The adapter checks the authentication method before running a model
command and reports a mismatch instead of starting a command with a conflicting login restriction.

Existing runs keep the authentication saved with them. After changing the default from ChatGPT to
API key, start a **new** task instead of resuming a run saved with ChatGPT authentication. To return
to ChatGPT, sign in with `codex login` (or `codex login --device-auth` in a headless container), select
**ChatGPT login**, and save. Orchestrator does not automatically switch the CLI's stored identity.
Claude and Antigravity continue to require their existing account logins.

## Your first Jira task in the TUI

The left panel is where you set up a task. The right panel shows progress and results. You can
click fields and buttons, and scroll the left panel to reach settings below the task description.
Try **Offline demo** first if you want to practice without provider credentials or model quota.

1. **Open the project you want to change.** Run from your Orchestrator checkout:

   ```sh
   uv run orchestrator tui --repo /workspace/backend
   ```

   Replace `/workspace/backend` with your project's absolute path. In Docker, use a path inside
   the container and an interactive terminal (`docker run -it`). The project must be a Git
   repository with at least one commit. In the **Repository** field, confirm that this is the
   project affected by your Jira ticket.

2. **Describe the Jira work.** Open the ticket and copy its title, description, acceptance
   criteria, and useful comments into **What should be changed?** Include relevant attachment
   details as text. For example:

   ```text
   Implement Jira ticket BACKEND-123: Handle empty order exports.

   Problem:
   Exporting orders crashes when the selected date range has no orders.

   Expected behavior:
   Return a valid CSV containing the column headers and no data rows.

   Acceptance criteria:
   - An empty date range does not cause an error.
   - The CSV still contains its headers.
   - Exporting existing orders continues to work.
   - Add a regression test for an empty export.

   Additional instructions:
   Follow the existing project conventions.
   Keep the public API unchanged.
   ```

   Automatic Jira import is not built in. An issue key or link alone is sufficient only when the
   selected agent actually has a working Jira tool; see [Jira MCP access](#jira-mcp-access).

3. **Choose your agents under MODELS.** Scroll down the left panel. The **Planner** investigates
   the project and proposes steps, the **Worker** changes code, and the **Reviewer** checks the
   result. Keep the defaults if those CLIs are installed and logged in, or select the same
   available provider for all three roles. Check the model field after changing a provider.
   In Docker, provider commands and logins must be available to the container user.
   For a Codex API-key login, scroll further to **AUTHENTICATION** and select **API key** for that
   named provider. See [Codex API-key authentication](#codex-api-key-authentication) for setup.
   To let a Codex or Claude planner fetch a ticket, enable **Use configured MCP servers** under
   **Planner**. For Claude, also allow the required Jira tools as described below.

4. **Choose skills if your task needs them.** Open **Skills**, enable the appropriate discovery
   options, and click **Refresh skill catalog**. Use `*` in a role's available-skills field to
   offer every discovered skill. To apply a workflow on every worker invocation, enter its
   frontmatter name, such as `atrium-interfaces`, under **Worker: required skills**. Discovery
   follows each role's provider; **Extra skill folders** can share a package across providers.
   Enter extra folders as a JSON array, such as `["/workspace/team-skills"]`. Skills describe
   workflows; adding a skill does not connect its MCP servers.

5. **Set your test commands under VERIFICATION.** Scroll further down the left panel. Enter
   commands for the project being changed, using JSON arrays with one entry per command word.
   For the terminal command `python3.9 -m pytest`, enter:

   ```json
   [["python3.9", "-m", "pytest"]]
   ```

   To run tests and lint:

   ```json
   [
     ["python3.9", "-m", "pytest"],
     ["ruff", "check", "."]
   ]
   ```

   These commands run in the generated task worktree. Make the required tools and dependencies
   available there; the original checkout's virtualenv is not copied. Leave **Maximum revision
   rounds** at `2` initially: this allows two revisions after the first implementation.
   Click **Save settings** to reuse the configuration. This saves provider, skill, and verification
   settings; it does not save the task description or repository selection as defaults.

6. **Click Start task.** Scroll back to the top of the left panel. The workflow runs automatically:
   **Plan → Implement → Verify → Review**.

   | Tab | What it shows |
   | --- | --- |
   | **Activity** | Progress, agent messages, check output, and errors. |
   | **Plan** | Proposed steps and acceptance criteria. |
   | **Review** | The run report, verification results, and review findings. |
   | **Diff** | Saved code changes when the run produces its diff. |
   | **History** | Previous runs you can reopen. |

7. **Add instructions or pause when needed.** Use **Additional instructions for the next stage**
   at the bottom of the right panel, then click **Send**. For example:

   ```text
   Also cover a date range containing only cancelled orders.
   ```

   **Pause** (`Ctrl+P`) finishes the current stage and pauses before the next one. **Stop**
   (`Ctrl+X`) interrupts active execution and keeps partial work. **Resume** (`Ctrl+R`) continues
   an unfinished run, retrying its pending stage. These controls also work with buttons.
   **Quit** (`Ctrl+Q`) stops active execution and exits. Settings changed in the left panel apply
   to new tasks; resumed runs keep their saved configuration and skill snapshots.

8. **Inspect the result before finishing the ticket.** The **RUN OVERVIEW** displays a worktree
   path: a separate working copy containing the changes. Open that path in your editor and inspect
   **Diff** and **Review**. Your original checkout's uncommitted changes are not copied into it.

   | Status | What to do |
   | --- | --- |
   | **Completed** | Review accepted; inspect the changes and verification results. |
   | **Needs attention** | The revision limit was reached; inspect the remaining findings. |
   | **Failed** | Read the error in Activity, resolve its cause, and resume. |
   | **Paused** | Inspect progress or add instructions, then resume when ready. |

   Orchestrator retains the changes for inspection. Commit, merge, push, and update Jira yourself
   after checking the result. A completed run cannot be resumed; start a new task for further work.

## Jira MCP access

You can use the MCP connections already configured in your provider CLI. Configure and authenticate
the Jira server **inside the container, as the same user who runs Orchestrator**. Orchestrator uses
the CLI's configuration and credential stores; it does not copy connection secrets into its TOML
profile. A connection in a separate app or under a different user is not automatically shared.

For a Jira task, the simplest setup is to enable MCP for the **Planner**. It fetches requirements
and includes them in the plan; the worker and reviewer receive that plan and can work without their
own Jira connection.

1. Under **MODELS → Planner**, select Codex or Claude and enable **Use configured MCP servers**.
2. **For Claude**, enter the actual Jira MCP tools under **Claude MCP tools to allow
   (comma-separated)** if they are not already approved in your CLI settings. For example:

   ```text
   mcp__atlassian-jira-dc__*
   ```

   This example uses your server name `atlassian-jira-dc`; replace it if your CLI uses another name.
   The `*` approves every tool on that server, including any write tools it offers.
   Use individual read/search tool names when you want to approve only those operations. Existing
   CLI permission grants still apply, and existing deny rules are not overridden. Leaving this
   field empty reuses your existing grants without adding new approvals.
3. Click **Save settings**, then start a **new** task. Resuming an old run keeps its saved settings.
4. Put the following in **What should be changed?**, replacing the ticket key:

```text
Implement Jira ticket BACKEND-123.
Before planning, use the Jira MCP to fetch its title, description, acceptance
criteria, relevant comments, and linked requirements.
Include the fetched requirements and acceptance criteria in the plan so the worker
and reviewer can use them. If Jira is unavailable, report the failure instead of
guessing the requirements. Follow the existing project conventions and add tests.
Read Jira only; do not post comments, edit fields, or transition the ticket.
```

Watch **Activity** for MCP tool calls, authentication errors, or permission denials. Check **Plan**
to confirm the fetched acceptance criteria are present before relying on them. If access fails,
fix the CLI connection or permissions in the container, then resume the run. You can also fetch the
ticket in your connected interactive CLI and paste its details into the task field.

| Provider | Behavior |
| --- | --- |
| **Codex** | With MCP enabled, loads its existing user configuration, including MCP servers and their native tool permissions. The adapter still explicitly selects the model, login method, approval policy, and role sandbox. Other user settings also load. With MCP off, user configuration is ignored as before. |
| **Claude Code** | With MCP enabled, exposes configured MCP tools and adds the listed tool permissions. Its built-in role tool sets and `dontAsk` permission mode remain in effect: tools that require an unanswered prompt are denied. With MCP off, all MCP tools are explicitly disabled. |
| **Antigravity** | Uses its existing MCP configuration and grants as before. The TUI shows this as a disabled informational control because this adapter has no per-invocation MCP switch. |

Server commands, credentials, and permission grants must work without interactive prompts in the
generated worktree. User-scoped CLI connections are useful here: a Claude local-scope connection
tied only to the original checkout may not be available in a new worktree. Ensure its configuration
also applies there. For server setup and permission details, see the
[official OpenAI MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) and
[Claude MCP permissions](https://code.claude.com/docs/en/permissions#tool-specific-permission-rules).

The equivalent profile settings belong in the existing role section, for example:

```toml
[roles.planner]
provider = "claude"
model = "sonnet"
reasoning = "high"
use_mcp = true
mcp_tools = ["mcp__atlassian-jira-dc__*"]
```

For Codex, use `use_mcp = true` and leave `mcp_tools = []`; configure tool grants in Codex itself.
New and older profiles default to MCP off for Codex/Claude until you enable it. Skills and task
instructions describe what an agent should do; the MCP controls and native CLI grants determine
which tools it can use. Orchestrator continues to instruct agents not to modify issue trackers.

For an active unfinished run, use the instruction field and **Send** to add context for the next
stage. For reusable planner instructions, edit `instructions` in the existing `[roles.planner]`
section of your TOML profile, then restart the TUI. For example:

```toml
instructions = """
When a task references Jira, use the available Jira MCP to read its requirements.
Include the requirements and acceptance criteria in the plan for downstream roles.
If Jira access fails, report that clearly. Do not modify Jira.
"""
```

This instruction requires MCP to be enabled and the Jira connection to be working. Entering an
instruction alone does not connect a server or grant its tools permission.

## Python 3.9 containers

Python 3.9 is supported. Inside your existing container, run as the same normal user who configured
Codex/Claude and their MCP connections. Create a virtualenv under that user's home so installation
and updates do not require `sudo`. Run these commands from the Orchestrator checkout:

```sh
python3.9 -m venv "$HOME/.venvs/orchestrator"
"$HOME/.venvs/orchestrator/bin/python" -m pip install .
"$HOME/.venvs/orchestrator/bin/orchestrator" init
"$HOME/.venvs/orchestrator/bin/orchestrator" tui --repo /path/to/project
```

Skip `init` if your configuration already exists. Keep the same working directory, profile, and
state directory when changing virtualenvs to retain your settings and history.

With uv, use `uv sync --python 3.9` and the usual `uv run orchestrator ...` commands. The
installation selects compatible dependencies and adds TOML/async timeout backports on Python 3.9
and 3.10; Python 3.11+ uses the standard library.

The container needs Git, your selected provider CLIs on `PATH`, and their logins available to the
container user. Mount the target repository and persist the configured state directory to retain
history and worktrees. Start the container with `docker run -it` for the TUI. Run
`orchestrator demo` to check the installation without provider credentials.

## Updating Orchestrator in a container

Keep your existing `orchestrator.local.toml` and state directory. You do not need to run `init`
again when updating.

1. Quit the TUI with `Ctrl+Q`.
2. In the container, open your Orchestrator checkout and pull the latest changes from `main`:

   ```sh
   cd /path/to/orchestrator
   git pull --ff-only origin main
   ```

   Replace the path with your actual checkout. If Git reports local changes or diverging history,
   resolve that before continuing; this command will not discard your changes. If the checkout is
   mounted from the host, you can pull in that same checkout on the host instead.

3. Update using the same installation method you originally used.

   **For uv:**

   ```sh
   uv sync --python 3.9
   uv run orchestrator tui --repo /path/to/your/project
   ```

   **For the pip virtualenv from the container example above:**

   ```sh
   "$HOME/.venvs/orchestrator/bin/python" -m pip install --upgrade .
   "$HOME/.venvs/orchestrator/bin/orchestrator" tui --repo /path/to/your/project
   ```

   If your virtualenv lives elsewhere, use its Python and `orchestrator` executables instead.

4. To use Jira MCP, follow [Jira MCP access](#jira-mcp-access): enable **Use configured MCP servers**
   for the desired Codex or Claude role, enter any additional Claude tool grants, and click
   **Save settings**. Start a **new** task; existing runs retain their saved configuration.

If Orchestrator was copied into the image during `docker build`, update the source checkout used
for that build, rebuild the image, and recreate the container using your existing run or Compose
configuration. Preserve the mounts for your profile, provider credentials, repository, and state
directory so they are available to the replacement container.

### Permission denied when updating a virtualenv

An installation error such as `Permission denied: '/opt/orchestrator-venv/bin/orchestrator'` means
your user cannot update that virtualenv. The earlier `/opt` example could create a root-owned
installation. You can use the user-owned virtualenv above and keep your existing profile and state.

If `/opt/orchestrator-venv` is your dedicated Orchestrator environment and you want your container
user to maintain it, run this one-time ownership repair **as that normal user**:

```sh
sudo chown -Rh -- "$(id -u):$(id -g)" /opt/orchestrator-venv
chmod -R u+rwX /opt/orchestrator-venv
```

Then update from your Orchestrator checkout:

```sh
/opt/orchestrator-venv/bin/python -m pip install --upgrade .
/opt/orchestrator-venv/bin/orchestrator tui --repo /path/to/your/project
```

Run the TUI without `sudo`. Running it as root changes CLI discovery, credential/configuration
locations, and skill discovery, and can create root-owned Git references, logs, and worktree records
that prevent later tasks from running as your normal user.

## Configuration and profiles

`orchestrator init` creates `orchestrator.local.toml`. See
[orchestrator.example.toml](orchestrator.example.toml) for the full example. The TUI lets you choose
existing provider definitions, enter arbitrary model IDs, select reasoning, edit verification
commands, and save defaults. Role-specific instructions and provider definitions are configured in
TOML. Listed `models` provide input suggestions, not a restriction or online model catalog.

New configurations include a `claude` provider in the TUI selector. For an existing profile, add:

```toml
[providers.claude]
backend = "claude-cli"
auth = "login"
executable = "claude"
models = ["sonnet", "opus", "haiku"]
sandbox = false
```

Then select `claude` for any role, or set that role's `provider = "claude"` and `model = "sonnet"`
in TOML. Claude's native aliases follow the CLI's supported model versions; a full model ID pins the
version. Reasoning maps to `--effort`; supported levels depend on the selected model. The adapter
requires a Claude account login, checks `claude auth status`, streams activity, and uses structured
outputs for planner/reviewer responses. It does not use API-key or cloud-provider credentials.
Use a current Claude Code version supporting these flags. Claude Code is covered by subprocess
fixtures; live Claude validation has not been performed on this development machine.

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

## Skills

Skills are discovered automatically from each role's harness global folders:

- Codex: `~/.agents/skills` and `$CODEX_HOME/skills` (normally `~/.codex/skills`).
- Antigravity: `~/.gemini/antigravity-cli/skills`.
- Claude Code: `$CLAUDE_CONFIG_DIR/skills` (normally `~/.claude/skills`).
- Project skills: `.agents/skills` for all harnesses, plus `.codex/skills` for Codex and
  `.claude/skills` for Claude Code.

Inspect the available names before choosing required work skills:

```sh
uv run orchestrator skills
uv run orchestrator skills --role worker --json
```

The **Skills** tab shows the catalog, lets you configure discovery and extra folders, and provides
available/required selections for each role. Click **Refresh skill catalog** after changing the
repository or provider, then **Save settings** to persist your choices. A role's `skills = ["*"]`
offers all discovered skills; a list of names offers only those skills. `skills = []` offers none
through the orchestrator catalog. `required_skills` always includes the named workflows, even when
they are absent from the optional list.

For a worker that must follow a company skill, edit its existing TOML section:

```toml
[roles.worker]
provider = "antigravity"
model = "gemini-3.8-flash-medium"
reasoning = "medium"
skills = ["*"]
required_skills = ["company-coding-rules"]

[skills]
inherit_global = true
discover_project = true
paths = []
```

Use the skill's frontmatter `name`, which may differ from its folder name. Missing selected or
required skills fail clearly before a task starts. Standard packages contain `SKILL.md` with YAML
`name` and `description`, plus optional `references/`, `scripts/`, and `assets/` folders.

Global discovery follows the role's selected backend. To share a Codex skill with an Antigravity
worker, or use another library, add its folder to `skills.paths`, for example
`paths = ["~/.codex/skills/company-coding-rules"]`. A root containing multiple skill packages or a
direct path to `SKILL.md` also works. Relative paths in a TOML profile resolve from the configuration
file's directory. For duplicate names, later sources win: explicit paths override project skills,
which override global skills. Linked skill folders are supported; linked resources must remain
inside the skill package.

Optional skills contribute their names, descriptions, and file locations to the role's prompt.
The agent chooses relevant workflows and reads their `SKILL.md` and references using file tools.
Required skills additionally contribute their instruction body directly to every invocation of
that role. Both use complete package copies saved with the run, so edits to your global library
affect new runs rather than silently changing a resumed run. Skill snapshots are checked for
modifications before use.

Copies are exposed inside a temporary workspace folder while an agent runs, then removed before
verification commands execute. They do not appear in the orchestrator's code diff. Saved snapshots
and their sources are recorded in the run's local history; ordinary optional selection remains a
model decision, and the model is instructed to state which skills it uses. Native harness discovery
may also expose its own skills; these controls configure the orchestrator catalog without changing
your harness's global settings. Skill instructions remain subject to the role's permissions and
response format. Tools, MCP connections, external credentials, and dependencies are not installed
or added just because a skill mentions them.

To verify actual skill and reference-file reads with your logged-in models, run:

```sh
uv run orchestrator doctor --live --skills
```

This uses a generated local test skill and consumes model quota; it does not send your work skills
to the models. Package snapshots are limited to 2000 files and 32 MiB per skill, excluding virtual
environments, Git internals, dependency caches, and Python bytecode.

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

**Execution policy:** Codex selects its read-only sandbox for planner/reviewer and workspace-write
sandbox for worker, with interactive escalation disabled. By default it runs without the user's
global Codex configuration. Enabling a role's MCP setting loads that configuration while retaining
explicit model/auth/sandbox flags; repository instructions still apply. Antigravity uses its native
`--mode plan` for planner/reviewer and `--mode accept-edits`
for worker, and preserves your existing permission rules. Plan mode requests analysis before edits;
it is not an OS read-only sandbox. The engine detects tracked/untracked source changes made by the
planner/reviewer and fails the stage for inspection. Existing Antigravity command, MCP, and web
grants still apply; headless requests requiring interactive approval are soft-denied by the CLI.
The adapter does not use `--dangerously-skip-permissions` and does not install global permission rules.
The orchestrator runs configured checks itself. `sandbox = true` adds Antigravity's native terminal
sandbox flag; it restricts terminal execution and does not guarantee read-only file tools.

Claude Code uses `dontAsk` to deny tools that would prompt. Planner/reviewer receive only
`Read`, `Glob`, and `Grep`; the worker additionally receives `Edit`, `Write`, and `Bash`, with file
tools approved for unattended use and edit approvals scoped to the worktree. Shell commands need
approval in your existing Claude permission rules, or the native Bash sandbox when `sandbox = true`.
That option enables sandboxed shell execution and disallows unsandboxed escape. It requires Claude's
platform sandbox dependencies.
MCP tools are disabled for Claude roles unless `use_mcp = true`; enabled roles reuse existing grants
and any additional `mcp_tools` rules. Native skills and project instructions still load;
skill scripts that need shell execution remain subject to the role's tool permissions. Verification
commands run separately through the orchestrator. No permission bypass flags are used.

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
- `skills.py`: harness discovery, per-role catalogs, pinned packages, and prompt loading.
- `adapters.py`: backend protocol and registry, Codex/Antigravity/Claude authentication implementations,
  offline fixture.
- `processes.py`: async streaming, output limits, timeouts, process-group cancellation.
- `git.py`: worktrees, diffs including untracked files, workspace fingerprints.
- `engine.py`: stages, structured contracts, checks, revisions, interruption/recovery.
- `store.py`: SQLite records, event history, artifacts, execution locks.
- `tui.py` / `cli.py`: terminal interface and headless commands.

Future direct API adapters implement `AgentBackend.run(AgentRequest, emit)` and
return an `AgentResult`. Add their configuration/capability validation and register the backend; the
workflow remains independent of provider SDKs. API adapters will also need their own scoped execution
tool loop. Direct API adapters, automatic Jira import, native model discovery, and concurrent
workers are **not implemented** in this version.

Integration references:
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[Antigravity headless mode](https://www.antigravity.google/docs/cli/headless/),
[Antigravity execution modes](https://www.antigravity.google/docs/cli/modes/),
[Antigravity permissions](https://www.antigravity.google/docs/permissions/),
[Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference),
[Claude Code headless mode](https://code.claude.com/docs/en/headless),
[Claude Code permissions](https://code.claude.com/docs/en/permissions).
Skill format references: [Codex skills](https://learn.chatgpt.com/docs/build-skills) and
[Antigravity skills](https://www.antigravity.google/docs/skills/) and
[Claude Code skills](https://code.claude.com/docs/en/skills).

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
