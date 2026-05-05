# Ratchet Code — Project CLAUDE.md

## file_tree

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
│       │   ├── context_builder.py
│       │   ├── executor.py
│       │   ├── plan_executor.py
│       │   ├── refiner.py
│       │   └── validator.py
│       ├── plan/
│       │   ├── __init__.py
│       │   ├── catalog.py
│       │   ├── planner.py
│       │   ├── schema.py
│       │   └── store.py
│       ├── __init__.py
│       ├── __main__.py
│       ├── chat.py
│       ├── claude_md.py
│       ├── config.py
│       ├── orchestrator.py
│       ├── solve.py
│       └── state.py
├── temp_validator_test/
├── .gitignore
├── =2.0
├── bench_rubric.md
├── CLAUDE.md
├── npm
├── output.json
├── pyproject.toml
├── ratchet.config.json
├── refiner_rubric.md
├── test_context_tools.py
├── TEST_INTEGRATION_README.md
├── test_orchestrated.py
├── test_prompt.txt
├── test_sdk.py
├── test_solve.py
├── test_validator_integration.py
├── test_validator_level_1.py
└── test_validator_levels_2_5.py
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
| `05_executor_validator.md` | `1OfO4AwUL1qE-KAUTVwsUEKCkyqKWlVhf` | Executor options construction (TOOLS_BY_STEP_TYPE sandbox, output_format, step prompt rendering), validator levels 1-5 (tools, turns, behavior). |
| `06_config.md` | `18M4af6OrIAY1rZelarGkChk3AQEj3yLB` | ratchet.config.json schema, Config Pydantic model (ExecutorModels, ValidationConfig, BudgetConfig), CLAUDE.md contract, solve() entry point, and SWE-bench adapter. |
| `RATCHET_OVERVIEW.md` | `1owEe267C-jofBb-KTwXiINxJ3GpNUEnQ` | Narrative overview: architecture rationale, data model summary, CLAUDE.md contract, configuration, non-obvious design decisions, and current status. |
| `RATCHET_SPEC.md` | `1qHl0HV5CIjgkLt5ERBGniXrOi-63OX5o` | Complete specification combining all numbered docs (01-06) into a single reference: data model, PlanStore rules, catalog tools, executor, validator, config, entry point, and critical API notes. |
| `RATCHET_THEORY.md` | `1iVWZMLMAh2d8222xajzJzNXndhNr4mt3` | Theoretical foundations: core hypothesis on phase separation, tool restriction vs prompt instruction, typed plans as tool calls, prerequisites as first-class concept, planner as intelligent layer, connection to Plan-and-Execute/MFR-PDDL literature, validation as configurable dimension, no repair loop rationale, and scope limitations. |
| `CLAUDE.md` (Drive copy) | `1InKa4Vg8mWociTT2MqUPQWS-b9Epl7XS` | Canonical CLAUDE.md template from the design docs. Defines file_tree, architecture (entities, module responsibilities, data flow, ContextVars, SDK notes), and restrictions. |

## architecture

### Entities and their locations

- `Task`, `TaskDescription`, `TaskStatus`, `TaskCategory` — `plan/schema.py`
- `Step`, `StepType`, `StepIntent`, `ValidatorSpec` — `plan/schema.py`
- `StepOutput`, `StepResult`, `ValidationVerdict`, `RefinementVerdict`, `ContextResult` — `plan/schema.py`
- `PlanStore`, `StepStatus` — `plan/store.py`
- `TaskStore` — `plan/store.py`
- `State` — `state.py`
- `Config`, `ExecutorModels`, `ValidationConfig`, `BudgetConfig`, `PlanningRules`, `RefinerConfig`, `ContextBuilderConfig` — `config.py`
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

#### RefinementVerdict

Returned by the plan refiner on `submit_plan`. Evaluates the plan against a rubric and returns either approval or rejection with actionable recommendations.

| Field | Type | Default | Description |
|---|---|---|---|
| `approved` | `bool` | required | Whether the plan meets the quality threshold |
| `score` | `int` (0-100) | required | Overall plan quality score; plans below the configured threshold are rejected |
| `diagnosis` | `str` | `""` | Explanation of the verdict and scoring rationale |
| `recommendations` | `list[str]` | `[]` | Actionable improvements for the planner to address |
| `rubric_scores` | `dict[str, int]` | `{}` | Per-dimension rubric scores; keys are dimension names, values 0-5 |
| `suggested_splits` | `list[str]` | `[]` | Step IDs that are too complex and should be split into multiple smaller steps |

#### ContextResult

Enriched context produced by the context builder agent. Returned by `build_context()` and prepended to the executor's `prev_context` so the executor has precise, repo-grounded information about the code it needs to modify.

| Field | Type | Default | Description |
|---|---|---|---|
| `enriched_context` | `str` | required | Markdown-formatted context block with function signatures, imports, file dependencies, and other relevant info |
| `files_read` | `list[str]` | `[]` | Files that were read during context building, for traceability |
| `functions_found` | `list[str]` | `[]` | Function/class signatures discovered in the target files |
| `notes` | `str` | `""` | Additional observations from context gathering |

### Module responsibilities

`__main__.py` — CLI entry point with two subcommands: `solve` and `chat`.
`solve` accepts `repo_path` plus `--prompt TEXT` or `--prompt-file PATH`,
`--model`, `--config`, `--instance-id`, `--max-turns`, `--cost-limit`.
Calls `solve()` and captures the `SolveResult` to emit a
`RATCHET_METRICS:{...}` JSON line with real token/cost/turn data to stdout
for the vexp-swe-bench harness. Also writes a `.ratchet.log` file (all
session logging) and a `.ratchet_plan.json` file (plan trace) next to the
repo directory. `chat` accepts an optional `repo_path` (defaults to cwd),
`--model`, `--config`. Starts an interactive REPL with the planner.
Installed as the `ratchet` console script via `pyproject.toml`.

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

`config.py` — `Config` and sub-models (`ExecutorModels`, `ValidationConfig`, `BudgetConfig`, `PlanningRules`, `RefinerConfig`, `ContextBuilderConfig`). Loads from `ratchet.config.json` via `Config.load(path)`. Provides `executor_model_for(step_type)` and `validator_model_for(level)` with fallback defaults. **Implemented**: all models, load(), executor_model_for(), validator_model_for(), max_validator_turns(), default_validation_level().

#### Configuration sections (`ratchet.config.json`)

The config file is divided into the following top-level sections:

**`models`** — Model selection for planner, executor, and validator.

| Key | Type | Description |
|---|---|---|
| `planner` | `str` | Model ID for the planner session |
| `executor` | `ExecutorModels` | Per-step-type model overrides (default, implement_step, update_docs_step) |
| `validator_by_level` | `dict[str, str \| null]` | Model per validation level (1-5); `null` for level 1 (subprocess) |

**`validation`** — Default validation level and turn budgets.

| Key | Type | Default | Description |
|---|---|---|---|
| `default_level` | `int` (1-5) | `3` | Default validation level when not specified |
| `max_turns_by_level` | `dict[str, int]` | `{"1":0,"2":1,"3":3,"4":5,"5":10}` | Max LLM turns per validation level |
| `level_by_step_type` | `dict[str, int]` | see code | Default validation level per step type |

**`budgets`** — Global budget limits for a single solve session.

| Key | Type | Default | Description |
|---|---|---|---|
| `max_executor_turns` | `int` | `20` | Max turns per executor step |
| `max_planner_turns` | `int` | `200` | Max turns for the planner session |
| `max_total_steps` | `int` | `50` | Max number of steps in a plan |

**`planning_rules`** — Enforced planning constraints checked at submit and execution.

| Key | Type | Default | Description |
|---|---|---|---|
| `require_discovery_before` | `list[str]` | `["implement_step"]` | Step types that require at least one completed discovery_step in their depends_on list |
| `max_steps` | `int` | `50` | Maximum number of steps allowed in a plan |
| `min_steps` | `int` | `0` | Minimum number of steps required in a plan |

**`refiner`** — Plan refinement agent configuration. The refiner runs on `submit_plan` and evaluates the plan against a rubric, returning approval or rejection with actionable recommendations. Disabled by default.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `false` | Whether the refiner agent is active |
| `model` | `str \| null` | `null` | Model ID for the refiner SDK session. Falls back to the planner model if not set |
| `rubric_path` | `str` | `"refiner_rubric.md"` | Path to the rubric markdown file used to score plans |
| `max_turns` | `int` | `3` | Max turns for the refiner SDK session |
| `min_score` | `int` (0-100) | `60` | Minimum score to approve a plan. Plans scoring below this are rejected with recommendations |
| `auto_reject_below` | `int` (0-100) | `30` | Plans scoring below this threshold are rejected outright with no recommendations |

The refiner scores plans across six dimensions (Goal Clarity, Dependency Correctness, Scope Appropriateness, File Coverage, Validation Alignment, Step Complexity), each 0-5, summed to a raw 0-30 score then scaled to 0-100. It returns a `RefinementVerdict` with `approved`, per-dimension `rubric_scores`, `recommendations`, and `suggested_splits` for steps that are too complex.

**`context_builder`** — Context builder agent configuration. The context builder runs before each step execution to enrich the step briefing with precise, repo-grounded information such as function signatures, import dependencies, and file relationships. Disabled by default.

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `false` | Whether the context builder is active |
| `model` | `str \| null` | `null` | Model ID for the context builder SDK session. Falls back to the executor model for the step if not set |
| `max_turns` | `int` | `5` | Max turns for the builder SDK session |

When enabled, the context builder runs a short SDK session with read-only tools (Read, Grep, Glob) to inspect the repository before each step execution. The resulting `ContextResult.enriched_context` is prepended to the executor's `prev_context` in `plan_executor.run_step()`, giving the executor precise, repo-grounded information before writing code.

`claude_md.py` — `parse_claude_md(repo_path) -> ClaudeMd`. Parses the three required sections (file_tree, architecture, restrictions). Raises `ValueError` if any section is missing.

`plan/catalog.py` — in-process MCP server (`ratchet_catalog`) with all custom tools: Task CRUD, Plan CRUD, and the `step` execution tool. All tools access state via `ContextVar`. Contains `check_prerequisites`. **Implemented**: 5 Task tools (task_create, task_get, task_update_what, task_update_status, task_delete), unified `add_step` tool (replaces per-type add_*_step tools), 9 other Plan tools (edit_step, remove_step, insert_step_after, mark_step_in_progress, mark_step_completed, mark_step_failed, view_plan, submit_plan, get_next_runnable), `step` execution tool, `check_prerequisites`, all 5 ContextVars. `bind_catalog_context(task_store, plan_store, state, repo_path, cfg)`. Tool names are registered without prefix (e.g. `task_create`); the orchestrator prefixes them with `mcp__ratchet_catalog__` for the CLI.

`exec/executor.py` — `execute_step(step, cfg, repo_path, state, restrictions, prev_context) -> StepResult`. Builds options with `TOOLS_BY_STEP_TYPE` sandbox, runs SDK client, captures StepOutput via text-based JSON parsing. **Implemented**: render_step_prompt, TOOLS_BY_STEP_TYPE mapping, structured output capture with regex-based JSON extraction.

`exec/validator.py` — `validate(step, result, cfg, repo_path) -> ValidationVerdict`. Level 1 is subprocess (auto-passes if no command provided); levels 2-5 are SDK clients with read-only sandbox. **Implemented**: all 5 levels, VALIDATOR_TOOLS_BY_LEVEL, render_validator_prompt. Levels 2-5 need a validator model configured in `ratchet.config.json` under `models.validator_by_level`.


`exec/plan_executor.py` — `run_step(step_id, prev_context, plan_store, state, cfg, repo_path, restrictions) -> dict`. Python function that implements the `step` tool body: resolve step, check_prerequisites, mark_in_progress, execute_step, validate, mark_completed/failed, return verdict JSON. **Implemented**: full step lifecycle.

`exec/context_builder.py` — `build_context(step, cfg, repo_path, deps) -> ContextResult`. Pre-execution context enrichment agent. Runs a short SDK session with read-only tools (Read, Grep, Glob) to inspect the repository and gather function signatures, import dependencies, test files, and other context relevant to the step being executed. The resulting `ContextResult.enriched_context` is prepended to the executor's `prev_context` in `plan_executor.run_step()` so the executor has precise, repo-grounded information before writing code. Guarded by `cfg.context_builder.enabled` (defaults to `False`). Uses a dedicated model from `cfg.context_builder.model`. Internal helpers: `_render_builder_prompt(step, deps)` builds the prompt from step metadata and prerequisite outputs; `_parse_context(msg)` extracts the `ContextResult` from the SDK result via regex-based JSON parsing.

`exec/refiner.py` — `refine(steps, rationale, cfg, repo_path) -> RefinementVerdict`. Plan quality review agent. Runs on `submit_plan` (called from `catalog.py`) to evaluate the plan against a configurable rubric (`refiner_rubric.md`). Runs a no-tool SDK session that scores the plan across six dimensions (Goal Clarity, Dependency Correctness, Scope Appropriateness, File Coverage, Validation Alignment, Step Complexity), each 0-5, scaled to an overall 0-100 score. Returns a `RefinementVerdict` with `approved` (score >= `cfg.refiner.min_score`), per-dimension `rubric_scores`, `recommendations` for the planner, and `suggested_splits` for steps that are too complex. Guarded by `cfg.refiner.enabled` (defaults to `False`). Uses a dedicated model from `cfg.refiner.model`. Plans scoring below `cfg.refiner.auto_reject_below` are automatically rejected. Internal helpers: `_render_plan_text(steps, rationale)` formats the plan for the refiner prompt; `_parse_verdict(msg)` extracts the `RefinementVerdict` from the SDK result.

`orchestrator.py` — starts the planner session as a live SDK client, injects the ratchet_catalog MCP server (with ContextVars bound to active PlanStore, TaskStore, State, Config, repo_path), streams messages to/from the user. Prefixes MCP tool names with `mcp__ratchet_catalog__` in `tools` and `allowed_tools` so the CLI recognizes and auto-approves them. Logs planner activity at INFO level. **Implemented**: `run_orchestrated(repo_path, request, cfg, model) -> SolveResult`.

`chat.py` — interactive REPL for the ratchet planner. Uses `ClaudeSDKClient` for persistent multi-turn conversation. Renders assistant messages, tool calls, thinking blocks, and results with ANSI formatting. Same tool set as the orchestrator (PLANNER_TOOLS + prefixed MCP catalog tools). **Implemented**: `run_chat(repo_path, cfg, model)`.

### Data flow

1. `ratchet solve <repo_path> --prompt ...` -> `solve(repo_path, request, config_path, model)`
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
| `plan/schema.py` | Done | All models: Task, TaskDescription, TaskStatus, TaskCategory, Step, StepType, StepIntent, ValidatorSpec, StepOutput, StepResult, ValidationVerdict, RefinementVerdict, ContextResult |
| `plan/store.py` | Done | TaskStore + PlanStore with all mutation rules from spec |
| `state.py` | Done | State with record(), resolve(), get() |
| `plan/catalog.py` | Done | 5 Task tools + 10 Plan tools + step execution tool + check_prerequisites + 5 ContextVars |
| `plan/planner.py` | Done | PLANNER_TOOLS (read-only), build_planner_options() |
| `config.py` | Done | Config, ExecutorModels, ValidationConfig, BudgetConfig, PlanningRules, RefinerConfig, ContextBuilderConfig. Load from JSON, executor_model_for(), validator_model_for(), max_validator_turns(), default_validation_level() |
| `__main__.py` | Done | CLI with solve/chat subcommands, --prompt/--prompt-file/--model/--config/--instance-id. File logging + plan trace |
| `solve.py` | Done | Single mode: orchestrated via run_orchestrated(). SolveResult with patch + metrics |
| `claude_md.py` | Done | Parse CLAUDE.md sections (file_tree, architecture, restrictions) |
| `exec/executor.py` | Done | TOOLS_BY_STEP_TYPE sandbox, render_step_prompt, text-based JSON output capture |
| `exec/validator.py` | Done | All 5 levels. Level 1 auto-passes without command. Levels 2-5 need validator model in config |
| `exec/plan_executor.py` | Done | run_step() with full lifecycle: resolve, check, execute, validate, update. Integrates context builder when enabled |
| `exec/context_builder.py` | Done | build_context() with read-only tools (Read, Grep, Glob). Returns ContextResult with enriched_context, files_read, functions_found. Guarded by cfg.context_builder.enabled |
| `exec/refiner.py` | Done | refine() with no-tool SDK session. Scores plans across 6 rubric dimensions (0-5 each), scaled to 0-100. Returns RefinementVerdict. Guarded by cfg.refiner.enabled |
| `orchestrator.py` | Done | run_orchestrated() with catalog MCP injection, ContextVar binding, MCP tool name prefixing |
| `chat.py` | Done | Interactive REPL with ClaudeSDKClient, ANSI rendering, persistent multi-turn planner session |

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
`src/agents/ratchet.ts`. It spawns `ratchet solve <repo_path> --prompt-file <tmp>`
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
- **Shell**: `C:\Program Files\Git\usr\bin\bash.exe`
- **Python**: `3.14.2` → `C:\Python314\python.exe`
- **Date/Time**: 2026-05-05 09:25:42 (SA Pacific Standard Time)
- **Unix Timestamp**: `1777991142`



### Extended System Info

- **LANG**: `unknown`
- **TERM**: `xterm`
- **PATH**:
  ```
  C:\Users\user\bin;C:\Program Files\Git\mingw64\bin;C:\Program Files\Git\usr\local\bin;C:\Program Files\Git\usr\bin;C:\Program Files\Git\usr\bin;
  ... C:\Users\user\.lmstudio\bin;C:\Program Files\Git\usr\bin\vendor_perl;C:\Program Files\Git\usr\bin\core_perl
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
│       │   ├── context_builder.py
│       │   ├── executor.py
│       │   ├── plan_executor.py
│       │   ├── refiner.py
│       │   └── validator.py
│       ├── plan/
│       │   ├── __init__.py
│       │   ├── catalog.py
│       │   ├── planner.py
│       │   ├── schema.py
│       │   └── store.py
│       ├── __init__.py
│       ├── __main__.py
│       ├── chat.py
│       ├── claude_md.py
│       ├── config.py
│       ├── orchestrator.py
│       ├── solve.py
│       └── state.py
├── temp_validator_test/
├── .gitignore
├── =2.0
├── bench_rubric.md
├── CLAUDE.md
├── npm
├── output.json
├── pyproject.toml
├── ratchet.config.json
├── refiner_rubric.md
├── test_context_tools.py
├── TEST_INTEGRATION_README.md
├── test_orchestrated.py
├── test_prompt.txt
├── test_sdk.py
├── test_solve.py
├── test_validator_integration.py
├── test_validator_level_1.py
└── test_validator_levels_2_5.py
```

### Project Stats

- **Python files**: 27
- **JS/TS files**: 0
- **Total tracked files**: 27

### Git Info

- **Branch**: `claude/download-claude-md-HTScD`
  - 8058dfe docs(claude_md): update file_tree section with current repo structure
  - 7bb2cc4 feat(cli): add chat subcommand and auto-pass level 1 validator without command
  - 55cf496 refactor(catalog): unify add_*_step into add_step, auto-fill defaults, pass restrictions

### Git Status

- **Modified**: 12
- **Staged**: 1
- **Untracked**: 17
- **Total**: 30 archivos



---