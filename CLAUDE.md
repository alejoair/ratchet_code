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

## design docs (Google Drive)

All design documents live in the "Ratchet Code" folder on Google Drive
(folder ID: `1Q4dIWjZnAv76DR2IKUvTROGysVAEALJk`).

| Document | Drive ID | Description |
|---|---|---|
| `01_overview.md` | `1XsyG_r3WDVOsfbx552vO8rM4jzZZWTq2` | High-level project purpose, layout, and three-agent architecture summary (planner, executor, validator). |
| `02_data_model.md` | `1aySkNXz6E4E-Iuj_P1b7h7OzRBJrhS51` | Canonical Pydantic v2 definitions for Task, Step, ValidatorSpec, StepOutput, StepResult, ValidationVerdict, and State. Source of truth for field names and types. |
| `03_planstore.md` | `1BkMfRQf5adOzJyUBKr6fCwciShuX7Pbq` | PlanStore class API (add_step, edit_step, remove_step, insert_step_after, mark_*, view, submit, next_runnable_id) and all mutation rules (lock, status guards, DAG constraints). |
| `03b_taskstore.md` | `1i3OKuJw6P56BS5ssfh9aeZBF9gzfRFLR` | TaskStore class API (create, get, update_*, set_*, view) and rules. Single active task per session, status lifecycle enforcement. |
| `04_catalog_tools.md` | `19fyHSpBiNE8JbdqbwCa2rNezbuyu8vpQ` | Full specification of the `ratchet_catalog` MCP server: Task tools (create_task, update_task_what, view_task), Plan tools (add_*_step, edit_step, remove_step, insert_step_after, view_plan, submit_plan), the `step` execution tool contract, and prerequisite check rules. |
| `05_executor_validator.md` | `1OfO4AwUL1qE-KAUTVwsUEKCkyqKWlVhf` | Executor options construction (TOOLS_BY_STEP_TYPE sandbox, output_format, step prompt rendering), validator levels 1-5 (tools, turns, behavior), and hooks (ruff_on_edit, commit_format_check, bash_whitelist, validator_no_write). |
| `06_config.md` | `18M4af6OrIAY1rZelarGkChk3AQEj3yLB` | ratchet.config.json schema, Config Pydantic model (ExecutorModels, ValidationConfig, BudgetConfig, HooksConfig), CLAUDE.md contract, solve() entry point, and SWE-bench adapter. |
| `RATCHET_OVERVIEW.md` | `1owEe267C-jofBb-KTwXiINxJ3GpNUEnQ` | Narrative overview: architecture rationale, data model summary, CLAUDE.md contract, configuration, non-obvious design decisions, and current status. |
| `RATCHET_SPEC.md` | `1qHl0HV5CIjgkLt5ERBGniXrOi-63OX5o` | Complete specification combining all numbered docs (01-06) into a single reference: data model, PlanStore rules, catalog tools, executor, validator, hooks, config, entry point, and critical API notes. |
| `RATCHET_THEORY.md` | `1iVWZMLMAh2d8222xajzJzNXndhNr4mt3` | Theoretical foundations: core hypothesis on phase separation, tool restriction vs prompt instruction, typed plans as tool calls, prerequisites as first-class concept, planner as intelligent layer, connection to Plan-and-Execute/MFR-PDDL literature, validation as configurable dimension, no repair loop rationale, and scope limitations. |
| `CLAUDE.md` (Drive copy) | `1InKa4Vg8mWociTT2MqUPQWS-b9Epl7XS` | Canonical CLAUDE.md template from the design docs. Defines file_tree, architecture (entities, module responsibilities, data flow, ContextVars, SDK notes), and restrictions. |

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

### Data models (`plan/schema.py`)

#### StepType (StrEnum)

Step classification used to select executor sandbox and model.

| Value | Description |
|---|---|
| `discovery_step` | Exploration and analysis, no code changes |
| `implement_step` | Primary code implementation |
| `simple_task_step` | Small, straightforward task |
| `verify_step` | Run tests or verification checks |
| `update_docs_step` | Documentation updates only |

#### StepIntent (StrEnum)

Semantic intent of a step, used for reporting and routing.

Values: `new_feature`, `modify`, `bugfix`, `refactor`, `test`, `config`, `docs`

#### ValidatorSpec

Validation configuration attached to each step.

| Field | Type | Default | Description |
|---|---|---|---|
| `level` | `int` (1-5) | required | Validation depth: 1 = subprocess, 2-5 = SDK client with increasing turn budgets |
| `success_criterion` | `str` | required | Human-readable pass/fail condition |
| `command` | `str \| None` | `None` | Shell command for level 1 validation |
| `extra_context` | `str \| None` | `None` | Additional context for levels 2-5 |

#### Step

Core unit of execution within a plan.

| Field | Type | Default | Description |
|---|---|---|---|
| `id` | `str` | required | Unique step identifier |
| `type` | `StepType` | required | Step classification (determines executor sandbox and model) |
| `goal` | `str` | required | Short description of what the step must accomplish |
| `briefing` | `str` | required | Detailed instructions for the executor agent |
| `target_files` | `list[str]` | `[]` | Existing files this step will modify |
| `creates_files` | `list[str]` | `[]` | New files this step will create |
| `deletes_files` | `list[str]` | `[]` | Files this step will delete |
| `depends_on` | `list[str]` | `[]` | Step IDs that must complete before this step |
| `validator` | `ValidatorSpec` | required | Validation spec for this step |
| `intent` | `StepIntent \| None` | `None` | Semantic intent hint |

#### StepOutput

Returned by the executor via `output_format`.

| Field | Type | Default | Description |
|---|---|---|---|
| `summary` | `str` | required | What the executor did |
| `artifacts` | `dict` | `{}` | Key-value metadata produced |
| `notes` | `str` | `""` | Additional observations |

#### StepResult

Internal struct used by the executor module before validation.

| Field | Type | Default | Description |
|---|---|---|---|
| `step_id` | `str` | required | Reference to the executed step |
| `output` | `StepOutput \| None` | required | Executor output if successful |
| `success` | `bool` | required | Whether execution completed without error |
| `error` | `str \| None` | `None` | Error message if execution failed |

#### ValidationVerdict

Returned by the validator. Levels 2-5 produce it via `output_format`; level 1 constructs it directly from `subprocess.run`.

| Field | Type | Default | Description |
|---|---|---|---|
| `passed` | `bool` | required | Whether validation succeeded |
| `diagnosis` | `str` | `""` | Explanation of the verdict |
| `suggested_fixes` | `list[str]` | `[]` | Actionable fixes if validation failed |

### Module responsibilities

`__main__.py` — CLI entry point. Accepts `repo_path` positional arg plus
`--prompt TEXT` or `--prompt-file PATH`, `--model`, `--config`.
Calls `solve()` and captures the `SolveResult` to emit a
`RATCHET_METRICS:{...}` JSON line with real token/cost/turn data to stdout
for the vexp-swe-bench harness. Also writes a `.ratchet.log` file (all
session logging) and a `.ratchet_plan.json` file (plan trace) next to the
repo directory. Installed as the `ratchet` console script via
`pyproject.toml`.

`solve.py` — `async def solve(repo_path, request, config_path, model) -> SolveResult`.
Entry point for library callers. Loads config (or builds a default),
delegates to `run_orchestrated()` which uses the full pipeline with
catalog MCP, executor, and validator. Returns a `SolveResult` dataclass
with patch + usage metrics. `SolveResult` is consumed by `__main__.py`
to emit `RATCHET_METRICS:{...}` to stdout.

`plan/planner.py` — Defines `PLANNER_TOOLS` (read-only: Glob, Grep, LS,
Read) and `build_planner_options(repo_path, model) -> ClaudeAgentOptions`.
The orchestrator builds its own options combining `PLANNER_TOOLS` with
prefixed MCP catalog tool names; `build_planner_options()` is available
for standalone planner sessions without catalog MCP.

`plan/schema.py` — all Pydantic models. No I/O, no side effects. Pure data definitions.

`plan/store.py` — `PlanStore` and `TaskStore`. Stateful, asyncio-locked. No SDK calls. No business logic beyond the rules defined in 03_planstore.md and 03b_taskstore.md. **Implemented**: TaskStore (create, get, update_status, update_description, delete, list_all) and PlanStore (add_step, edit_step, remove_step, insert_step_after, mark_in_progress, mark_completed, mark_failed, get_step, get_status, get_output, get_verdict, next_runnable_id, view, submit). All mutation rules from the spec are enforced.

`state.py` — `State` class. Holds logical outputs (StepOutput) indexed by step_id. No asyncio lock needed (single-writer: plan_executor). **Implemented**: record(), resolve(), get().

`config.py` — `Config` and sub-models (`ExecutorModels`, `ValidationConfig`, `BudgetConfig`, `HooksConfig`). Loads from `ratchet.config.json` via `Config.load(path)`. Provides `executor_model_for(step_type)` and `validator_model_for(level)` with fallback defaults. **Implemented**: all models, load(), executor_model_for(), validator_model_for(), max_validator_turns().

`claude_md.py` — `parse_claude_md(repo_path) -> ClaudeMd`. Parses the three required sections (file_tree, architecture, restrictions). Raises `ValueError` if any section is missing.

`plan/catalog.py` — in-process MCP server (`ratchet_catalog`) with all custom tools: Task CRUD, Plan CRUD, and the `step` execution tool. All tools access state via `ContextVar`. Contains `check_prerequisites`. **Implemented**: 5 Task tools, 10 Plan tools, `step` execution tool, `check_prerequisites`, all 5 ContextVars (`_task_store`, `_plan_store`, `_state`, `_repo_path`, `_config`). `bind_catalog_context(task_store, plan_store, state, repo_path, cfg)`. Tool names are registered without prefix (e.g. `task_create`); the orchestrator prefixes them with `mcp__ratchet_catalog__` for the CLI.

`exec/executor.py` — `execute_step(step, cfg, repo_path, state, restrictions, prev_context) -> StepResult`. Builds options with `TOOLS_BY_STEP_TYPE` sandbox, runs SDK client, captures StepOutput via text-based JSON parsing. **Implemented**: render_step_prompt, TOOLS_BY_STEP_TYPE mapping, structured output capture with regex-based JSON extraction.

`exec/validator.py` — `validate(step, result, cfg, repo_path) -> ValidationVerdict`. Level 1 is subprocess; levels 2-5 are SDK clients with read-only sandbox. **Implemented**: all 5 levels, VALIDATOR_TOOLS_BY_LEVEL, render_validator_prompt. Currently returns a "no model configured" verdict for levels 2-5 when no validator model is set in config.

`exec/hooks.py` — `build_hooks(cfg, step, is_validator=False) -> list`. Returns hook list for ruff_on_edit, commit_format_check, bash_whitelist, validator_no_write. **Implemented**: all 4 hooks, is_validator guard.

`exec/plan_executor.py` — `run_step(step_id, prev_context, plan_store, state, cfg, repo_path, restrictions) -> dict`. Python function that implements the `step` tool body: resolve step, check_prerequisites, mark_in_progress, execute_step, validate, mark_completed/failed, return verdict JSON. **Implemented**: full step lifecycle.

`orchestrator.py` — starts the planner session as a live SDK client, injects the ratchet_catalog MCP server (with ContextVars bound to active PlanStore, TaskStore, State, Config, repo_path), streams messages to/from the user. Prefixes MCP tool names with `mcp__ratchet_catalog__` in `tools` and `allowed_tools` so the CLI recognizes and auto-approves them. Logs planner activity at INFO level. **Implemented**: `run_orchestrated(repo_path, request, cfg, model) -> SolveResult`.

### Data flow

1. `ratchet <repo_path> --prompt ...` -> `solve(repo_path, request, config_path, model)`
2. `solve()` loads config (or builds default), delegates to `run_orchestrated()`.
3. Orchestrator creates TaskStore, PlanStore, State, binds ContextVars, builds catalog MCP.
4. Planner has read-only tools (Glob, Grep, LS, Read) plus prefixed MCP catalog tools.
5. Planner calls Task tools to define the task, then Plan tools to build the plan.
6. Planner calls `step(step_id?, prev_context?)` to execute each step.
7. `step` tool body -> check_prerequisites -> executor -> validator -> mark_completed/failed -> returns verdict JSON to planner.
8. Planner reacts to verdict, calls more plan tools or `step` as needed.
9. On session end, orchestrator runs `git diff` and returns the result.
10. `__main__.py` emits `RATCHET_METRICS:{...}` to stdout and writes `.ratchet_plan.json` and `.ratchet.log`.

### Implementation status

| Module | Status | Notes |
|---|---|---|
| `plan/schema.py` | Done | All models: Task, TaskDescription, TaskStatus, TaskCategory, Step, StepType, StepIntent, ValidatorSpec, StepOutput, StepResult, ValidationVerdict |
| `plan/store.py` | Done | TaskStore + PlanStore with all mutation rules from spec |
| `state.py` | Done | State with record(), resolve(), get() |
| `plan/catalog.py` | Done | 5 Task tools + 10 Plan tools + step execution tool + check_prerequisites + 5 ContextVars |
| `plan/planner.py` | Done | PLANNER_TOOLS (read-only), build_planner_options() |
| `config.py` | Done | Config, ExecutorModels, ValidationConfig, BudgetConfig, HooksConfig. Load from JSON, executor_model_for(), validator_model_for(), max_validator_turns() |
| `__main__.py` | Done | CLI with --prompt/--prompt-file/--model/--config. File logging (.ratchet.log) + plan trace (.ratchet_plan.json) |
| `solve.py` | Done | Single mode: orchestrated via run_orchestrated(). SolveResult with patch + metrics |
| `claude_md.py` | Done | Parse CLAUDE.md sections (file_tree, architecture, restrictions) |
| `exec/executor.py` | Done | TOOLS_BY_STEP_TYPE sandbox, render_step_prompt, text-based JSON output capture |
| `exec/validator.py` | Done | All 5 levels (subprocess + SDK client). Levels 2-5 need validator model in config |
| `exec/hooks.py` | Done | ruff_on_edit, commit_format_check, bash_whitelist, validator_no_write |
| `exec/plan_executor.py` | Done | run_step() with full lifecycle: resolve, check, execute, validate, update |
| `orchestrator.py` | Done | run_orchestrated() with catalog MCP injection, ContextVar binding, MCP tool name prefixing |

All modules implemented. The MVP is complete and verified end-to-end on SWE-bench instances.

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

**MCP tool name prefixing**: The CLI prefixes MCP tool names as
`mcp__<server_name>__<tool_name>`. Both `tools` and `allowed_tools` must
use these prefixed names (e.g. `mcp__ratchet_catalog__task_create`) for the
CLI to recognize and auto-approve them. The catalog server itself registers
tools without the prefix (e.g. `task_create`); the orchestrator adds the
prefix when building `ClaudeAgentOptions`.

### vexp-swe-bench integration

The harness adapter lives in the cloned `vexp-swe-bench` repo at
`src/agents/ratchet.ts`. It spawns `ratchet <repo_path> --prompt-file <tmp>`
and parses the `RATCHET_METRICS:` line from stdout. Registered in
`src/agents/registry.ts` as `"ratchet"`. Includes cross-platform kill
signal handling (Windows compatibility).

To run a benchmark subset:
```bash
# Install ratchet (Windows)
pip install -e C:\Users\user\Desktop\ratchet_code

# Dry run (verify adapter loads and instances match)
cd C:\Users\user\Desktop\vexp-swe-bench
node dist/cli.js run --agent ratchet --dry-run --no-vexp

# Run one instance
node dist/cli.js run --agent ratchet --instances django__django-11133 --no-vexp

# Run multiple instances
node dist/cli.js run --agent ratchet --instances id1,id2,id3 --no-vexp

# Linux/Docker
python3.12 -m venv /opt/ratchet-venv
/opt/ratchet-venv/bin/pip install -e .
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

## Project Context (Auto-generated)

> **Nota**: Esta sección se genera automáticamente antes de cada query.
> No la edites manualmente ya que se sobrescribirá.
>
> Providers activos: generate_system_context, generate_extended_system_context, generate_filetree_context, generate_stats_context, generate_git_context, generate_git_status_context

### System Info

- **OS**: 🪟 Windows 11 (AMD64)
- **User**: `user@DESKTOP-92K2Q7P`
- **Home**: `C:\Users\user`
- **Shell**: `C:\WINDOWS\system32\cmd.exe`
- **Python**: `3.14.2` → `C:\Python314\python.exe`
- **Date/Time**: 2026-05-03 10:44:33 (SA Pacific Standard Time)
- **Unix Timestamp**: `1777823073`



### Extended System Info

- **LANG**: `unknown`
- **TERM**: `unknown`
- **PATH**:
  ```
  C:\Python314\Scripts\;C:\Python314\;C:\WINDOWS\system32;C:\WINDOWS;C:\WINDOWS\System32\Wbem;
  ... C:\Users\user\AppData\Roaming\Python\Python314\Scripts;C:\Users\user\AppData\Local\Programs\Microsoft VS Code\bin;C:\Users\user\.lmstudio\bin
  ```



### File Tree

```
ratchet_code/
├── bench_results/
│   ├── astropy-14369.jsonl
│   └── evaluation.md
├── examples/
│   └── swebench_adapter.py
├── src/
│   └── ratchet/
│       ├── exec/
│       │   ├── __init__.py
│       │   ├── executor.py
│       │   ├── hooks.py
│       │   ├── plan_executor.py
│       │   └── validator.py
│       ├── plan/
│       │   ├── __init__.py
│       │   ├── catalog.py
│       │   ├── planner.py
│       │   ├── schema.py
│       │   └── store.py
│       ├── __init__.py
│       ├── __main__.py
│       ├── claude_md.py
│       ├── config.py
│       ├── orchestrator.py
│       ├── solve.py
│       └── state.py
├── .gitignore
├── =2.0
├── bench_rubric.md
├── CLAUDE.md
├── npm
├── pyproject.toml
├── ratchet.config.json
├── test_orchestrated.py
├── test_prompt.txt
├── test_sdk.py
└── test_solve.py
```

### Project Stats

- **Python files**: 21
- **JS/TS files**: 0
- **Total tracked files**: 21

### Git Info

- **Branch**: `claude/download-claude-md-HTScD`
  - 99b6e3a fix(exec): replace output_format with text-based JSON parsing for SDK compat
  - f78a530 feat: complete MVP with orchestrated pipeline, config, executor, validator, and hooks
  - f3c71cd feat(solve): return structured SolveResult with token/cost metrics

### Git Status

```
  M CLAUDE.md
   M src/ratchet/__main__.py
   M src/ratchet/orchestrator.py
   M src/ratchet/plan/planner.py
   M src/ratchet/solve.py
  ?? =2.0
  ?? bench_results/
  ?? bench_rubric.md
  ?? npm
  ?? test_orchestrated.py
  ?? test_prompt.txt
  ?? test_sdk.py
  ?? test_solve.py
```

---