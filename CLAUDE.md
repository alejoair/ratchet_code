# Ratchet Code — Project CLAUDE.md

## file_tree

```
ratchet/
├── pyproject.toml
├── ratchet.config.json
├── CLAUDE.md
├── src/ratchet/
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py
│   ├── claude_md.py
│   ├── state.py
│   ├── solve.py
│   ├── orchestrator.py
│   ├── plan/
│   │   ├── __init__.py
│   │   ├── store.py
│   │   ├── schema.py
│   │   ├── catalog.py
│   │   └── planner.py
│   └── exec/
│       ├── __init__.py
│       ├── executor.py
│       ├── validator.py
│       ├── hooks.py
│       └── plan_executor.py
└── examples/
    └── swebench_adapter.py
```

## architecture

### Entities and their locations

- `Task`, `TaskDescription`, `TaskStatus`, `TaskCategory` — `plan/schema.py`
- `Step`, `StepType`, `StepIntent`, `ValidatorSpec` — `plan/schema.py`
- `StepOutput`, `StepResult`, `ValidationVerdict` — `plan/schema.py`
- `PlanStore`, `StepStatus` — `plan/store.py`
- `TaskStore` — `plan/store.py`
- `State` — `state.py`
- `Config`, `ValidationConfig`, `BudgetConfig`, `HooksConfig` — `config.py`
- `ClaudeMd` — `claude_md.py`

### Module responsibilities

`__main__.py` — CLI entry point. Accepts `repo_path` positional arg plus
`--prompt TEXT` or `--prompt-file PATH`, `--model`, `--max-turns`,
`--cost-limit`, `--config`. Calls `solve()` and emits a
`RATCHET_METRICS:{...}` JSON line to stdout for the vexp-swe-bench harness.
Installed as the `ratchet` console script via `pyproject.toml`.

`solve.py` — `async def solve(repo_path, request, config_path, model) -> str`.
Entry point for library callers. Builds planner options via
`build_planner_options()`, runs a one-shot `query()` session, logs the
`ResultMessage` (turns, cost), and returns `git diff HEAD`.
Until the orchestrator and catalog are wired up, this is the sole execution
path and calls the planner directly.

`plan/planner.py` — `build_planner_options(repo_path, model) -> ClaudeAgentOptions`.
Configures the planner session: full Claude Code toolset (`PLANNER_TOOLS`),
`permission_mode="acceptEdits"`, `allowed_tools=PLANNER_TOOLS`,
`setting_sources=["user", "project", "local"]`. Does not run the session.

`plan/schema.py` — all Pydantic models. No I/O, no side effects. Pure data definitions.

`plan/store.py` — `PlanStore` and `TaskStore`. Stateful, asyncio-locked. No SDK calls. No business logic beyond the rules defined in 03_planstore.md and 03b_taskstore.md.

`state.py` — `State` class. Holds logical outputs (StepOutput) indexed by step_id. No asyncio lock needed (single-writer: plan_executor).

`config.py` — `Config` and sub-models. Loads from `ratchet.config.json`. Provides `executor_model_for(step_type)` and `validator_model_for(level)`.

`claude_md.py` — `parse_claude_md(repo_path) -> ClaudeMd`. Parses the three required sections (file_tree, architecture, restrictions). Raises `ValueError` if any section is missing.

`plan/catalog.py` — in-process MCP server (`ratchet_catalog`) with all custom tools: Task CRUD, Plan CRUD (add_*_step, edit_step, remove_step, insert_step_after, view_plan, submit_plan), and the `step` execution tool. All tools access state via `ContextVar`. Contains `check_prerequisites`.

`exec/executor.py` — `execute_step(step, cfg, repo_path, state, restrictions, prev_context) -> StepResult`. Builds options with `TOOLS_BY_STEP_TYPE` sandbox, runs SDK client, captures StepOutput via `output_format`.

`exec/validator.py` — `validate(step, result, cfg, repo_path) -> ValidationVerdict`. Level 1 is subprocess; levels 2-5 are SDK clients with read-only sandbox and `output_format`.

`exec/hooks.py` — `build_hooks(cfg, step) -> list`. Returns hook list for ruff_on_edit, commit_format_check, bash_whitelist, validator_no_write.

`exec/plan_executor.py` — not an agent. Python code that implements the `step` tool body: resolve step, check_prerequisites, mark_in_progress, execute_step, validate, mark_completed/failed, return verdict JSON.

`orchestrator.py` — starts the planner session as a live ClaudeSDKClient, injects the ratchet_catalog MCP server (with ContextVars bound to active PlanStore, TaskStore, State, Config, repo_path), streams messages to/from the user.

### Data flow

#### Current (planner-only)

1. `ratchet <repo_path> --prompt ...` → `solve(repo_path, request, model)`
2. `solve()` calls `build_planner_options()` and runs `query(prompt, options)`.
3. Planner has full Claude Code toolset and edits the repo directly.
4. On session end, `solve()` runs `git diff HEAD` and returns the patch.

#### Target (full pipeline — not yet implemented)

1. User message → orchestrator → planner session (live SDK client).
2. Planner calls Task tools to define the task, then Plan tools to build the plan.
3. Planner calls `step(step_id?, prev_context?)` to execute each step.
4. `step` tool body (plan_executor.py) → executor → validator → updates PlanStore + State → returns verdict JSON to planner.
5. Planner reacts to verdict, calls more plan tools or `step` as needed.
6. On session end, orchestrator runs `git diff` and returns the result.

### ContextVar bindings

All ContextVars are set once by the orchestrator before starting the planner session:

```python
_plan_store: ContextVar[PlanStore]
_task_store: ContextVar[TaskStore]
_state: ContextVar[State]
_config: ContextVar[Config]
_repo_path: ContextVar[str]
```

They are defined in `plan/catalog.py` and read by every tool in `ratchet_catalog`.

### SDK key distinction

`tools` in `ClaudeAgentOptions` = real sandbox (what the LLM sees).
`allowed_tools` = auto-approval list (what runs without a permission prompt).
Always set both to the same list. Never rely on `disallowed_tools` alone to sandbox.

`permission_mode="bypassPermissions"` is blocked by the Claude Code CLI when
running as root. Use `"acceptEdits"` combined with a full `allowed_tools` list
to achieve equivalent auto-approval behavior.

### vexp-swe-bench integration

The harness adapter lives in the cloned `vexp-swe-bench` repo at
`src/agents/ratchet.ts`. It spawns `ratchet <repo_path> --prompt-file <tmp>`
and parses the `RATCHET_METRICS:` line from stdout. Registered in
`src/agents/registry.ts` as `"ratchet"`.

To run a benchmark subset:
```bash
# Install ratchet in a Python 3.12 venv
python3.12 -m venv /opt/ratchet-venv
/opt/ratchet-venv/bin/pip install -e .
ln -sf /opt/ratchet-venv/bin/ratchet /usr/local/bin/ratchet

# Run 3 instances
cd vexp-swe-bench
node dist/cli.js run --agent ratchet --instances id1,id2,id3 --no-vexp
```

## restrictions

- Python 3.12+. No walrus operator in type annotations.
- Pydantic v2 for all models. Use `model_validate`, `model_dump`, `model_json_schema`. Never use v1 patterns (`__fields__`, `validator` decorator).
- asyncio throughout. No threading. No `asyncio.run` inside async functions.
- `contextvars.ContextVar` for all shared mutable state in catalog tools. No module-level mutable globals.
- Type hints on all functions and methods, including return types. `mypy --strict` must pass.
- Docstrings on all public classes and functions. Google-style docstrings.
- No emojis anywhere in code or comments.
- Ruff for linting and formatting. Run `ruff check --fix` and `ruff format` before committing.
- Commit message format: `type(scope): description` where type is one of feat, fix, refactor, docs, chore. Max 72 chars in the description. Example: `feat(catalog): add insert_step_after tool`.
- No print statements in library code. Use Python `logging` module with `logger = logging.getLogger(__name__)`.
- All errors raised must be typed exceptions. No bare `raise Exception(...)`.
- `claude-agent-sdk>=0.1.69` pinned in pyproject.toml.
- `pydantic>=2.0` pinned in pyproject.toml.
