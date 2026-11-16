# Evaluation methodology

See also: [documentation map](../README.md#documentation) · [planted defects](planted-defects.md) · [sample matrix](../results/matrix.md) · [eval pages in the UI](../frontend/src/pages/evals/README.md)

How the model-provider evaluation runs, what each published figure measures, and where its
boundaries are. A sweep writes its figures to `matrix.md`, `matrix.json` and `matrix.csv` in its
output directory. Under `results/`, `matrix.json` and `matrix.csv` are gitignored, and the tracked
`results/matrix.md` is the published live sweep of the default matrix, both families. It reflects
its recorded run: each arm's Provenance table gives the run's timestamp, and the tier comparison
summary gives each tier's models and efforts, which can differ from the current defaults if those
changed after the run. A stub sweep writes to the gitignored `results/stub/` by default and refuses to write
into `results/`, so it cannot overwrite the published file. This document does not reproduce
figures, because a figure is only meaningful attached to one run's provenance and raw archive.

**Measurement only.** The evaluation reports correctness, cost, latency and behavioural figures per
model arm. It never declares a winner, a verdict, or a recommendation between Anthropic and OpenAI
models, and no script or gate in this repository is permitted to. `--assert-artifacts` fails a sweep
whose artifacts render a verdict.

## The default sweep

`{Anthropic, OpenAI} × {effort medium} × 24 golden tasks`, which is 48 agent runs per repeat.
`--repeats N` (default 1) runs every task N times per arm, so 5 repeats is 240 runs.

Each arm is one family's three tiers, as `config/models.yaml` configures them. `effort medium` is the
supervisor tier's effort; the subagent and tool tiers both run at `low`, the efforts
`config/models.yaml` gives them. The effort scales are not identical across families:
`effort_scale_comparability.compared_models` in `config/model_capabilities.yaml` states, per tier,
each compared model's rungs and default, and `matrix.md` cites it. No arm sets a temperature; the
tuning wire format (schema 3) has no such parameter.

`src/eval/harness.py:run_sweep` is the one function both the CLI and the UI trigger call, so the two
cannot diverge in what they measure. Golden tasks live in `src/eval/golden_tasks.yaml`.

**Unit-tenant isolation.** The harness and the `eval_sweep` state machine both run every
(arm, repeat) unit in its own freshly seeded tenant, `eval-<run_id>-a<arm>r<repeat>`, so no unit
observes another unit's writes: arm 2 and repeat 2 start from the seeded values, not from what arm 1
wrote. Tasks run in the yaml order in every unit. The run's `eval_runs` row records its unit tenants
before any is seeded. The harness tears every unit tenant down in a `finally`, and a teardown failure
propagates. In the state machine, `sweep_control` seeds the unit tenants in its Seed step and tears
them all down in its Teardown step, which refuses a recorded set that is not exactly the run's own.
`scripts/reap_eval_tenants.py` reaps the unit tenants an orphaned run recorded. The `demo` and
`canary` guards apply to each unit tenant.

By default the sweep uses the stub provider (`src/providers/stub.py`): a deterministic, scripted,
zero-inference response generator whose output is a function of the resolved model id. It produces
different, reproducible per-arm scores, but it is **not** a measurement of either vendor's model.

**Reproducibility.** The same arguments, including the same `dataset_seed`, produce identical
artifacts **except for the `timestamp` provenance field**, which is set from `datetime.now(UTC)` per
invocation. `tests/unit/test_harness.py` states the contract in that form. Read any "byte-identical"
claim elsewhere in the repository as carrying the same exception.

A deployed sweep runs in the `eval_sweep` Step Functions state machine (see `docs/runbook.md`). Its
trigger accepts `repeats` from 1 to `MAX_SWEEP_REPEATS` (`src/eval/runs.py`), default 1; the live
estimate is multiplied by it, and every (arm, repeat, task) is one item of the state machine's Map:

![A deployed eval sweep: the Evals page, part of the React SPA that CloudFront serves from a private S3 bucket, calls eval_trigger through CloudFront /api/* and API Gateway; eval_trigger starts the eval_sweep state machine, whose sweep_control Lambda seeds and tears down the run's unit tenants, one per arm and repeat, aggregates, and writes artefacts to S3, and whose sweep_task Lambda runs each task as one eval_turn on the AgentCore Runtime, which calls Bedrock models and the CRM action Lambdas. An inset lists the state machine's states.](figures/eval-methodology-aws-sweep.svg)

The default matrix, and what `run_sweep` does with it:

![The default sweep matrix: 2 arms (Anthropic, OpenAI) times effort medium times 24 golden tasks times 1 repetition equals 48 agent runs per repeat; run_sweep seeds one tenant per arm and repeat, scores each run without a judge model, tears every unit tenant down in a finally, and renders matrix.json, matrix.md and matrix.csv with a per-tier comparison (results/stub/ for a stub sweep, results/ for a live one). Measurement only, no verdict.](figures/eval-methodology-sweep-matrix.svg)

### Running a stub sweep

Free, offline, no credentials. The CLI default targets DynamoDB Local
(`scripts/run_local_dynamodb.sh start`, needs Docker):

```bash
.venv/bin/python3 -m src.eval.harness          # writes results/stub/
```

`--output-dir results` is refused for an all-stub sweep (`StubMatrixOverwriteRefused`), because
`results/matrix.md` holds the published live sweep.

Without Docker, the same `run_sweep` call can run against an in-process `moto` backend, as
`tests/stub/test_stub_sweep.py` does. `run_sweep` takes a `Store` and does not branch on the backend
behind it:

```bash
.venv/bin/python3 -c "
import boto3
from moto import mock_aws
from crm.store.provisioning import create_all_tables
from crm.store.store import Store
from eval.harness import DEFAULT_ARM_SPECS, run_sweep
with mock_aws():
    client = boto3.client('dynamodb', region_name='us-east-1')
    create_all_tables(client)
    result = run_sweep(store=Store(client=client), arm_specs=DEFAULT_ARM_SPECS)
    for arm_run in result.arm_runs:
        print(arm_run.arm_result.provenance.model_id, arm_run.arm_result.aggregate_score_fields)
"
```

Useful CLI flags: `--output-dir`, `--archive-raw` (write each arm's raw JSON under `<output-dir>/raw/`),
`--assert-artifacts` (read every published artifact back and fail on any absent, malformed or
verdict-rendering one; implies `--archive-raw`), `--repeats N`, `--dataset-seed`, `--table-prefix`.

### Live mode

Reaching live models takes two explicit opt-ins that must agree:

- `--live` selects live **models**: one real family adapter per arm, built through
  `providers.factory.build_provider_for_family` and reached over `bedrock-runtime` with the
  `global.`-prefixed inference profile for every tier.
  - The Anthropic adapter streams every tier over the Messages API through `anthropic`'s
    `AsyncAnthropicBedrock`.
  - The OpenAI adapter streams over the Responses API through `openai`'s `AsyncBedrockOpenAI`, at an
    explicit `bedrock-runtime` base URL on `/openai/v1/responses`.

  Both are IAM/SigV4 only, signed from the boto3 session. No vendor API key and no Bedrock API key is
  read, held, or accepted; the SDKs' bearer-token and `bedrock-mantle` fallbacks are refused.
- `--substrate live-aws` (or `EVAL_SUBSTRATE=live-aws`) selects live **AWS**: the deployed DynamoDB
  tables and a real `lambda:InvokeFunction` per tool call.

`--live` without the live substrate exits 2 without invoking anything, and `run_sweep(mode="live")`
with the offline dispatcher raises `LiveSubstratePairingError` before it seeds a row.

Billable command (real model inference plus writes to deployed tables under a disposable tenant):

```bash
EVAL_SUBSTRATE=live-aws \
CRM_TABLE_PREFIX=crm-agent-eval \
ACTION_LAMBDA_PREFIX=crm-agent-eval \
  .venv/bin/python3 -m src.eval.harness --live --assert-artifacts --max-cost-usd <usd>
```

**Cost cap.** `--max-cost-usd` is required with `--live`. Before each (arm, repeat) unit starts, the
harness compares the priced spend of the completed units plus the most expensive completed unit with
the cap. When that sum exceeds the cap, no further unit starts, every tenant is torn down, no matrix
is written, and the exit code is 4. Priced cost is computed from recorded tokens against
`config/pricing.yaml`, so it estimates the bill; the Bedrock bill is the authority.

**Account precondition.** `--live` needs the account's Bedrock data-retention mode to permit the
OpenAI models; at `mode: none` every GPT-6 invocation is rejected. Check it read-only with
`aws bedrock get-account-data-retention --region <region> --no-cli-pager`. The setting is
account-wide, so changing it is the account owner's decision. See `docs/runbook.md`.

**Transient failures.** `eval.live.ThrottleRetryingProvider` retries a throttled turn with bounded
exponential backoff. Every retried attempt, whether retried by the vendor SDK or by this wrapper,
becomes exactly one exclusion, categorised by its cause: `throttle` for a throttle, `retry`
otherwise. The wrapper retries an in-band error by its leading code: throttle and availability
codes (Bedrock's `ThrottlingException`, `ServiceUnavailableException` and similar, and the Responses
surface's `service_unavailable`) count as `throttle`; transient server faults (Bedrock's
`InternalServerException`, `ModelErrorException`, `ModelStreamErrorException`, and the Responses
surface's `server_error`) count as `retry`. A failure outside the closed exclusion taxonomy (cold
start, retry, throttle) raises `eval.live.LiveInvocationError` and aborts the sweep, so no artifact
is published with an uncategorised gap.

**Runtime coverage.** A CLI `--live` sweep records `runtime_identifier: live-aws-lambda-dispatch`:
live models, live DynamoDB and real Lambda dispatch are exercised; the deployed AgentCore Runtime's
own turn loop is not. A deployed sweep (the `eval_sweep` state machine) runs each task as an
`eval_turn` on the Runtime and records `runtime_identifier: agentcore-runtime-eval-turn`
(`src/eval/sfn_handlers.py` `RUNTIME_IDENTIFIER`).

### The eval turn budget

A deployed sweep (the `eval_sweep` state machine, see `docs/runbook.md`) runs each task as one
`eval_turn` on the AgentCore Runtime, under the caps in `src/agents/budget.py`: at most 25 tool
calls, 1,000,000 tokens and a 120-second wall clock. The 120-second cap is soft. When it is reached,
every pending model wait ends, the turn stops with `error_kind` `budget_exhausted`, and the partial
turn is scored against committed state like any other, with `budget_exhausted: true` on its result.
Each task score also records `budget_exhausted_tier`, the tier whose budget ran out (`null` when it
did not), and `repeat_index`, the repeat the run belonged to (from 0), so a raw archive keeps both
per run rather than only as per-arm counts.
At 150 seconds (30 seconds past the soft cap) the Runtime cancels the turn at its next await and
answers 504, so a turn that cannot unwind in time fails rather than hanging the sweep.

**Leaf calls are charged as they are dispatched.** Every leaf tool call inside a delegation is
charged against the turn's 25-call cap before it runs. At the cap the leaf is refused rather than
run, the refusal is logged as `tool_call_refused`, and ToolCall is recorded as the exhaustion tier.
The delegation stops there and returns what it had gathered, so a turn can no longer overrun the
cap inside a delegation and be charged only after it returns. The supervisor's direct calls count
against the same cap as they complete, and its loop stops the turn when the cap is reached.

### The tool-output bound

Every tool result is bounded before a model sees it, on every path: the supervisor's direct calls,
leaf calls inside a delegation, and provider-reported tool events. Size is measured as the UTF-8
length of the result's Python `repr`, not its JSON. A result over 32,768 bytes
(`TOOL_RESULT_BYTE_CEILING` in `src/agents/events.py`) keeps as many whole records of
its record list as fit, gains a truncation marker record, and carries `truncated: true`. A result
under the ceiling is passed unchanged. The model receives the bounded copy, and the tool call's
attribution records the same flag, so the truncation rows of the tier comparison count what the
model was actually given. A delegation one of whose leaves was bounded reports itself truncated.

The dataset makes the bound reachable. Planted defect PD-08 adds 55 activities to `opp-0000`, so even
that opportunity's 50 smallest activities together exceed the ceiling. `gt-05`
asks for every activity on `opp-0000` and so exercises truncation: its listing reaches the model
truncated and disclosed, and a summary that hides the truncation is scored as `silent_truncation`.
`gt-01` and `gt-17`, which also read `opp-0000`, keep the fields they score after the bound.

**`gt-04` fits the budget.** `gt-04` is scoped to rep-ashford's opportunities on `acct-0000` and
expects reads of all four whose primary contact (`cont-0000`) has departed (`opp-0000`, `opp-0040`,
`opp-0080` and `opp-0200`). It completes within 20 tool calls whether the work is done directly or
in two research delegations, so it measures a multi-record investigation inside the 25-call cap.

A model call or round that the deadline cut off, or that the turn stopped, before it reported usage
leaves its tokens uncounted, so that turn's usage and cost are a **lower bound**. A call cut off after
its usage arrived is counted as metered, and does not make the usage a lower bound. The observed turn carries
`usage_lower_bound: true`, and each affected arm lists those task runs in
`usage_lower_bound_task_ids`, with a note under its figures in `matrix.md`.

### Per-request prompt limits

Every model request, chat or eval, is checked before it is sent against its tier's limit in
`config/models.yaml` `request_prompt_limits`: 270,000 estimated prompt tokens at the supervisor and
subagent tiers, and 95,000 at the tool tier. The three are independent settings, and each must fit
the context window (`context_window_tokens` in `config/model_capabilities.yaml`) of every model that
can be assigned to its tier; the capability matrix refuses to load otherwise. A request over its
limit is **refused, never trimmed**: nothing is sent, and the turn ends with its budget exhausted
on the `request_prompt_limit` dimension, naming the estimate, the limit and the tier.
Separately, a turn may use at most 1,000,000 input plus output tokens in total.

The estimate is `ceil(utf8_bytes / 3)` over the compact JSON of the messages and tool schemas
handed to the model call (`src/agents/prompt_estimate.py`). It uses no tokenizer, so it is the same
for every family, and it omits framing an adapter adds afterwards (its own system blocks or
tool-choice wrappers). Error bound: English prose and JSON tokenize at about 3.5 to 4.5 bytes per
token, so for them the estimate runs about 17 to 50 percent high and refuses early: a real prompt
of roughly 180,000 to 231,400 tokens can be refused at an estimate of 270,000. Dense numerics, ids or
non-Latin text can tokenize below 3 bytes per token, so the estimate can run low and admit a request
larger than the limit. Such a request is still priced from the usage the model reports, at
long-context rates when it crosses the model's threshold. The tool limit sits below Haiku 5.5's
100,000-token threshold, so a tool call the estimate admits is priced at standard rates unless the
estimate under-counts.

## What the models are sent

Every family receives the same texts; no prompt is selected by family.

- **Supervisor.** Each chat and eval turn opens with two system messages: the supervisor prompt,
  [`config/agent_prompts/supervisor.yaml`](../config/agent_prompts/supervisor.yaml), then the answer
  style, [`config/answer_style.yaml`](../config/answer_style.yaml), which covers formatting only. A
  turn with page context adds a third system message, the page briefing, which quotes the
  client-supplied values as data. `src/agents/turn_driver.py` builds this sequence.
- **Subagents.** Each delegation's system message is
  [`config/agent_prompts/subagent.yaml`](../config/agent_prompts/subagent.yaml), rendered with the
  subagent's `role` and `purpose` from `agents.subagents.base.SubagentSpec`. The supervisor's intent
  follows as the user message. A subagent returns its tool results in call order as
  `{"results": [{"tool": ..., "body": ...}, ...]}`, bounded by the tool-output byte ceiling.
- **Tool tier.** The tool-actor directive (`src/agents/tool_actor.py`) and the intent form one user
  message.
- **Tools.** Every tool definition comes from `src/crm/toolspec/definitions.py`.
- **Error results.** A failed tool call returns `error.code`, `error.message` and `error.next_step`.
  `next_step` is one of `fix_arguments`, `tell_user`, `refresh_then_ask_user`, `retry_later` and
  `stop`, set per error class in `src/crm/errors.py`.

**Prompt bundle fingerprint.** `agents.prompt_bundle.prompt_bundle()` collects everything above,
together with the subagent tool schemas, the page-briefing templates and the prompt-caching
mechanism of each family (`providers.prompt_caching.CACHING`). Its SHA-256 over canonical JSON,
`prompt_bundle_sha256()`, is recorded in every arm's provenance as `prompt_bundle_sha256`, in
`matrix.json`, `matrix.md` and the raw archive. An arm whose task units ran under different bundles
raises rather than folding them into one result. Archives written before the field existed read it
as `null`.

Two sweeps measure the same instrument only when their `prompt_bundle_sha256` values are equal. Any
edit to a prompt file, the answer style, a tool description or a caching mechanism changes the
digest, so sweeps from before and after such an edit are different instruments and are not
comparable figure for figure. That includes every sweep recorded before the digest existed.

## Tier comparison summary

`matrix.md` opens with a per-tier summary of the sweep's arms. It comes directly after the title,
before the intro line, `## Reading these figures` and the per-arm sections, which follow it
unchanged. `matrix.json` carries the same figures as its first key, `tier_comparison`, with
`columns`, `tiers` (`supervisor`, `subagent`, `tool`) and `notes`. `matrix.csv` carries them as
`summary_*` columns after every existing column, with each arm's values repeated on each of its task
rows. `eval.matrix.tier_comparison` builds all three from the arm records. The summary tables
themselves come from `eval.matrix.tier_comparison_table`, which the CLI's `matrix.md` and the eval
dashboard both render. The dashboard shows every header, cell and note as verbatim text; the only
markdown it honours is a note's inline code spans, which it renders as code.

There are three tables, Supervisor, Subagent and ToolCall, each with one column per arm in run order.
Columns are never re-sorted. A column is headed by the arm's family. When a family has more than one
arm, the header is `family (model_id, effort E)`, and ` [arm N]` is added to a header that still
repeats. An arm archived without a recorded family is headed by its model id.

Every table has these rows, in this order:

| Row | Definition, per arm, at the tier |
|---|---|
| Model | Every model id at the tier, sorted and comma-separated: the configured model and every model that spent tokens there. |
| Effort | The distinct efforts the tier ran at, taken from the resolved tiers each turn reports. `unsupported` is a model that takes no effort setting; `—` means no turn recorded an effort. |
| Task completion rate | `rate (met/applicable)` over the arm's tasks applicable at the tier; `not applicable` when no task is. Supervisor: the task's correctness is 1.0. Subagent: the delegation outcome is the expected one. ToolCall: an expected tool was called with schema-valid arguments. Counted per turn, so with repeats each repeat of a task counts once. |
| Task completion over repeats | `rate (completed/applicable, majority of N)`. A task counts as completed at the tier when it passed in a strict majority of the repeats in which the tier was applicable (`passes × 2 > applicable repeats`), and as applicable when the tier applied in any repeat. With one repeat it equals the task completion rate. `n/a` when an archive has no tier scores. |
| Task accuracy | Supervisor: the arm's `mean_correctness`. Subagent and ToolCall: the completion rate. |
| Cost per completed task | The tier's total cost divided by its completed-task count, including spend on tasks that did not complete; `—` at zero completions. |
| P50 latency per completed task (ms) | Per task completed at the tier, the sum of that tier's measured call latencies in the task; nearest-rank P50 over those tasks, shown with n. |
| P95 latency per completed task (ms) | As above, at P95. |
| Model invocations | Included invocations at the tier. |
| Long-context invocations | The tier's included invocations priced at the invoked model's long-context rates (see Usage and cost). |
| Total input tokens | The tier's whole prompt, summed per call: reported input plus cache read plus cache write for Anthropic, and reported input for OpenAI, or that call's cache read plus cache write where they exceed it (see Usage and cost). Counted the same way for every family. |
| Cache read tokens | The tier's prompt tokens read from the prompt cache. |
| Cache write tokens | The tier's prompt tokens written to the prompt cache. |
| Uncached input tokens | Total input tokens minus Cache read tokens minus Cache write tokens: the prompt tokens neither read from nor written to the cache. Total input tokens = Cache read + Cache write + Uncached input, exactly, in every column. |
| Total output tokens | The tier's output tokens, reasoning tokens included. |
| Cost per applicable task | The tier's total cost divided by the number of task runs applicable at the tier (the denominator of the task completion rate); `—` when no task run is applicable. At 100% completion it equals Cost per completed task. |
| Self-corrected retries | The tier's tool calls whose `tool_failure` result was self-corrected (see the retry rule below). `not applicable` at ToolCall, which makes one call per invocation. |

Every table then ends with three more rows:

| Row | Definition, per arm, at the tier |
|---|---|
| Budget-exhausted tasks | The arm's task runs whose turn ran out of budget at this tier. |
| Truncated tool calls | The tool calls issued at this tier whose result reported truncation. `not applicable` at Subagent. |
| Truncated tasks | The arm's task runs with at least one truncated tool call at this tier. `not applicable` at Subagent. |

A turn that runs out of budget is counted at exactly one tier, the tier where the exhaustion was
first detected; a later detection in the same turn does not move it.

- A model request refused because its estimated prompt is above the per-request limit counts at the
  tier of the refused request.
- A model call cut off by the turn's wall-clock deadline counts at that call's tier.
- A subagent that stops at a round boundary because the deadline passed counts at Subagent.
- A leaf call inside a delegation refused at the tool-call cap counts at ToolCall.
- Running out of the turn's tool-call allowance in the supervisor's own loop, or of its token
  allowance, counts at Supervisor, as does any other exhaustion the supervisor's loop detects first.

A truncated tool call is counted at the tier that issued it. The supervisor's direct tool calls
count at Supervisor, and every call made inside a delegation, whichever role made it, counts at
ToolCall, so both truncation rows read `not applicable` at Subagent. Every call records the
truncation flag of the bounded result its model received (see The tool-output bound).

In `matrix.json` the three rows are the tier entry's `budget_exhausted_task_count`,
`truncated_tool_call_count` and `truncated_task_count` (`null` at Subagent for both truncation
counts). In `matrix.csv` they are `summary_<tier>_budget_exhausted_task_count`,
`summary_<tier>_truncated_tool_call_count` and `summary_<tier>_truncated_task_count`, with the
Subagent truncation cells empty.

Delegation outcome counts (`data`, `no_match`, `denied`, `tool_failure` and `none`, over every task
the arm ran) are not a table row. They are in `matrix.json`, as the subagent entry's
`delegation_outcomes`, and in `matrix.csv`, as the `summary_subagent_delegation_*` columns. `none`
counts turns that reported no recognised outcome, so the counts sum to the arm's task count.

Reading rules:

- **Self-corrected retries.** A tool call that returned `tool_failure` is self-corrected when a
  later call in the same task, to the same tool from the same role, returned `data` or `no_match`.
  A self-corrected failure does not set the task's delegation outcome, so the outcome is scored on
  the final result (`eval.metrics.retried_failure_positions`). The subagent reply path applies the
  same rule. A `denied` result is never cleared. The count is `retry_count` in `matrix.json` and
  `summary_<tier>_retry_count` in `matrix.csv`.
- **Latency samples.** A task completed at a tier without a measured call at that tier contributes
  no sample (the supervisor's direct read tools can set the delegation outcome without a subagent
  call), so n counts only contributing tasks. A call with no recorded latency is skipped within its
  task. Percentiles use the nearest-rank method (see Latency below).
- **Stub arms.** The stub provider calls no model, so both latency rows read `not measured`. The
  Model row still names the configured model at each tier.
- **Withheld cost.** While `config/pricing.yaml` is `provisional: true`, both cost rows read
  `withheld` in `matrix.md`, their `matrix.json` values are `null` with `cost_withheld_provisional:
  true`, and their `summary_*` CSV cells are empty. `scripts/check_cost_withheld.sh` accepts the
  summary's non-cost numeric fields and still reports any numeric cost value.
- **No comparison.** Columns are the sweep's arms in run order, not sorted by any result. No
  difference or ratio between columns is computed, and no cell is emphasised. The notes under the tables give the definitions, and state that effort labels are not
  equivalent across families (`effort_scale_comparability` in `config/model_capabilities.yaml`) and
  that input tokens are not like-for-like (`token_accounting_disclosure` in `src/eval/matrix.py`).
  Further notes appear when they apply: stub columns, withheld cost, errored items, and arms whose
  usage is a lower bound.
- **Older records are refused.** Every arm record, `matrix.json` and its `tier_comparison` block
  carries `record_shape_version` as its first key, currently 3 (3 added the per-run
  `budget_exhausted_tier` and `repeat_index` to each task score). A record without it, with any other
  value, or missing a required field (such as a tier's `total_prompt_tokens`, `cache_read_tokens` or
  `cache_write_tokens`) is refused with `RecordShapeRefused`, naming the version found and the one
  required. Nothing is rendered from it: `python -m eval.matrix` and `eval.reprice` exit 2 and write
  no file, and `tier_comparison_table` raises. Records from earlier sweeps are not upgraded or
  partly rendered.

## Correctness figures

Defined in `src/eval/scoring.py` (`score_task`, `aggregate_scores`). Every figure is a plain rate;
none is a pass/fail verdict. No judge model is used anywhere in scoring.

| Figure | Definition |
|---|---|
| `mean_correctness` | Mean, over scored tasks, of each task's fraction of satisfied machine-checkable assertions. Unscored assertions are excluded from numerator and denominator. |
| `scored_task_count` / `unscored_task_count` | Tasks with and without a correctness value. A task is unscored when its `scoring_mode` is `prose_unscored`, or when it is a `machine` task declaring no positive assertion (`expected_field_changes`, `expected_proposal`, `expected_denial`, `expected_reads`). |
| `bad_write_rate` | Fraction of all tasks (scored or not) where a baseline-backed `must_not_change` assertion, or an `allowed_*` proposal-field assertion, failed. |
| `tool_call_correct_rate` | Fraction of tasks whose turn issued at least one tool call and did not propose outside the task's proposal scope. |

Boundaries that matter when reading them:

- **Both write paths are checked.** Low-risk writes are scored against committed state; high-risk
  changes are scored against pending proposal records, never against committed state.
- **A pre-turn baseline is required.** `must_not_change` and the `__advanced__` sentinel compare
  against values captured by `capture_pre_turn_values` before the turn. A missing baseline makes the
  assertion `unscored`, never a pass. `matrix.json`'s `figure_semantics.per_arm_bad_write_rate_basis`
  publishes how many assertions were baseline-backed per arm.
- **`mean_correctness` is not a task-success rate.** Most assertions are negative guards a do-nothing
  turn satisfies, so the figure's range is compressed well above zero.
  `figure_semantics.per_arm_correctness_composition` states each arm's positive-versus-guard mix,
  including `positive_assertion_satisfaction_rate`.
- **`tool_call_correct_rate` does not measure tool choice.** It fails only on no tool call, a
  `submit_proposal` on a zero-proposal task, or a proposal targeting the wrong record.
- **`expected_reads` measures retrieval, not report quality.** It passes when at least one call to a
  named tool reached the named record; for an entry with `target_id: null` (whole-book tools such as
  `pipeline_aggregate`, `list_proposals`) it checks only that a named tool was called. It is a
  necessary condition for an apt retrieval, not a sufficient one.
- **Report quality is measured nowhere.** `gt-04` and `gt-05` score retrieval in place of report
  quality, because grading prose would need a judge model. Each narrowed task's `notes` field in
  `golden_tasks.yaml` states its boundary. `figure_semantics.retrieval_narrowing_magnitude` and
  `per_arm_retrieval_conservatism` size the deduction this conservatism makes to `mean_correctness`.

`results/matrix.md` and `matrix.json` carry these boundary statements themselves, rendered from one
function in `src/eval/matrix.py`; `--assert-artifacts` fails when the two publish different text.

## Usage and cost

Defined in `src/eval/metrics.py` (`ModelMetrics.to_report_fields`). Per arm:

- `included_invocation_count`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_write_tokens`, and `cache_min_checkpoint_tokens` (from `config/model_capabilities.yaml`).
- `exclusions`: `total_excluded` and `by_category`, with every category (`cold_start`, `retry`,
  `throttle`) present even at zero. An excluded invocation leaves the latency/cost statistics but
  is always tallied; no sample is dropped silently.
- Cost fields (`cost_input`, `cost_output`, `cost_cache_write`, `cost_cache_read`,
  `cost_excluding_cache`, `cost_cache_total`, `cost_total`, `price_table_version`), derived from
  recorded tokens against `config/pricing.yaml`.

**Per-call pricing.** Every model call is priced on its own. Its prompt size is input plus cache
read plus cache write tokens. When the invoked model has a `long_context` block in
`config/pricing.yaml` and the prompt size exceeds its `above_prompt_tokens`, every token class of
that call, output included, is priced at the long-context rates; a call exactly at the threshold is
standard. The thresholds are:

| Model | Long-context above (prompt tokens) |
|---|---|
| GPT-6.1 Sol, GPT-6.1 Terra, GPT-6 Luna, GPT-6 Astra | 272,000 |
| Claude Haiku 5.5 | 100,000 |
| Claude Sonnet 5.5, Claude Opus 5.5 | none (no long-context tier) |

Every rate, standard or long-context, is then multiplied by the route of the call's inference
profile (`src/crm/inference_route.py`): a `global.` profile is the global route at the base rate
(`routes.global.multiplier` 1.0), and a geographic prefix (`us.`, `eu.`, `apac.`, `ca.`, `jp.`, `au.`)
is the geo route at `routes.geo.multiplier` 1.10. A profile with any other prefix, or one naming a
different model than the call's, is refused rather than priced at a guessed rate: the capability
matrix refuses to load it, and re-pricing refuses the call.

Each cost block also counts `long_context_invocation_count`, its included invocations priced at
long-context rates, per arm, per tier and per (tier, role, model) row.

**Arm figures.** An arm's `model_metrics` (its Usage and cost block in `matrix.md`) covers only the
supervisor-tier calls to the arm's model, and is labelled as such where it is rendered. The arm
totals, `arm_cost_total` and `arm_long_context_invocation_count`, cover every call the arm made at
every tier, so `arm_cost_total` equals the sum of the three tiers' costs in the tier comparison.
`matrix.md` shows them as an "All tiers" table under each arm. `matrix.json` carries them as
arm-level keys of those names; `matrix.csv` carries them as the `arm_cost` and
`arm_long_context_invocation_count` columns.

Rates come from `config/pricing.yaml` (`price_table_version` 1.3.0), global-route on-demand
Standard rates for short context, plus the long-context rates and route multipliers above. The
Anthropic models and GPT-6 Astra are read from the AWS Price List
(`aws pricing get-products --service-code AmazonBedrockFoundationModels`, region us-east-1).
GPT-6.1 Sol, GPT-6.1 Terra and GPT-6 Luna are absent from the us-east-1 Price List, so their rates
come from the AWS Bedrock model cards (Global CRIS, short context). The file's `sourced_from` and
`sourced_on` name the exact query and pages.
To refresh the table, re-read the same sources, transcribe the standard and long-context rates, the
long-context thresholds and the route multipliers, bump `price_table_version`, and update
`sourced_from` and `sourced_on`.

**Re-pricing a stored sweep.** A sweep archived with `--archive-raw` keeps each call's tokens and
inference profile, so it can be re-priced against the current table without calling a model:

```bash
.venv/bin/python3 -m src.eval.reprice --raw-dir <sweep>/raw --output-dir <out>
```

It writes `matrix.{json,md,csv}` and `reprice.json` into `<out>`. A call recorded without an
inference profile is refused unless `--assume-inference-profile-prefix <prefix>` is given, in which
case its profile is `<prefix><model_id>`; the assumption is printed to stderr and recorded in
`reprice.json`. An arm whose per-call records disagree with its archived token totals, per
(tier, role, model) row and per tier, is refused. Exit code 0 means the files were written, 2 that
the input was refused.

`config/pricing.yaml` carries `provisional: false`, so absolute cost is published and
`cost_withheld_provisional` is `false`. If the table is set back to `provisional: true`, cost fields
are omitted (not zeroed), `cost_withheld_provisional` becomes `true`, and
`scripts/check_cost_withheld.sh` fails the build on any absolute currency figure in a published
artifact.

Each family reports cached prompt reads in a differently shaped usage object, so `input_tokens` is
not like-for-like across arms: Anthropic reports input excluding cache read and cache write tokens,
OpenAI reports input including them. `matrix.md` states the per-family accounting
(`token_accounting` in `figure_semantics`, sourced from `src/crm/metering.py`).

**Tier token totals.** Each tier of the tier comparison renders five token rows: Total input
tokens (`total_prompt_tokens`), Cache read tokens (`cache_read_tokens`), Cache write tokens
(`cache_write_tokens`), Uncached input tokens (`uncached_input_tokens`) and Total output tokens
(`output_tokens`), summed over the tier's (tier, role, model) rows. A call's prompt size is
`crm.metering.prompt_tokens`: input plus cache read plus cache write for Anthropic, and the reported
input for OpenAI, which already contains the cache counts (raised to their sum if they exceed it).
So each call's prompt is at least its cache read plus cache write, and the uncached figure is never
negative. Uncached input is derived, not recorded: `eval.matrix.uncached_input_tokens` computes
`total_prompt_tokens - cache_read_tokens - cache_write_tokens` when the summary is built and again
when a stored summary is rendered, so a stored record-shape-3 run without the field renders the
same row. Because the subtraction is linear, the tier figure equals the sum of the per-call figures,
and Total input = Cache read + Cache write + Uncached input holds exactly in every column for both
families. A given prompt yields the same Total, cache and uncached figures under either family.

The tier tables carry no reported-input row. Each family's own `input_tokens` stays in the
per-model Usage and Cost tables, in `matrix.json` (`tier_comparison.tiers.<tier>[].input_tokens`)
and in `matrix.csv` (`summary_<tier>_input_tokens`). The JSON entry carries
`uncached_input_tokens` beside `total_prompt_tokens`, and the CSV carries
`summary_<tier>_uncached_input_tokens`.

**Tier cost rows.** Both cost rows divide the tier's total cost, including spend on task runs that
did not complete. Cost per completed task divides by the task runs completed at the tier; Cost per
applicable task divides by the task runs applicable at the tier. They are equal when every
applicable run completed. In `matrix.json` they are `cost_per_completed_task` and
`cost_per_applicable_task`; in `matrix.csv`, `summary_<tier>_cost_per_completed_task` and
`summary_<tier>_cost_per_applicable_task`.

## Latency (P50/P95)

Latency percentiles use the **nearest-rank** method (`eval.metrics.nearest_rank_percentile`): for a
sample of n observations sorted ascending, the p-th percentile is the value at 1-indexed rank
`ceil(p/100 * n)`. No interpolation, so every reported figure is an observed sample.

- Task latency is the wall-clock of each golden-task run; call latency is the wall-clock of each
  model invocation, grouped by tier (`supervisor`/`subagent`/`tool`) and reported as
  `{p50, p95, count}`.
- Call latency is the `latency_ms` the provider adapter reports on its `usage` event, measured with a
  monotonic clock from the successful attempt's request to the end of the stream.
- A throttled or retried invocation is not a latency sample, as it is not a cost sample.
- Report fields: `latency_measured`, `latency_percentile_method` (`nearest-rank`),
  `task_latency_ms_p50`, `task_latency_ms_p95`, `call_latency_ms_by_tier`.
- Stub sweeps make no model call, so they report `latency_measured: false` with null figures, which
  keeps stub artifacts identical apart from the timestamp.

## Eval-only fault injection

gt-16 (`gt-16-denial-tool-failure-not-conflated`) needs a real tool failure to check that a failure is
never reported as a denial or as missing data. Its yaml entry declares
`fault_injection: {tool: list_activities, fault: transport}`. `src/eval/fault_injection.py` reads that
declaration from the packaged registry, keyed on the task id, and `eval.turn_observation.observe_turn`
wraps the turn's dispatch with it. Every call to the named tool in that turn then goes through the real
`crm.dispatch.invoke_action` with a client that cannot connect, so the turn sees the real
`internal_error` payload, which classifies as `tool_failure`. A retry fails the same way.

The fault cannot reach any user channel:

- Under `src/`, `eval.turn_observation` is the only importer of `eval.fault_injection`. No handler, Gateway, relay or agent
  module imports it, and a unit test asserts that.
- Activation needs an `eval-<run_id>` tenant (a `demo`, `canary` or run-id-less tenant raises
  `FaultInjectionRefused`) and a task id the registry declares a fault for. A caller-supplied payload
  cannot declare a fault.
- The chat path and the Runtime's `delegation` mode never call `observe_turn`.

## The oracle gate

`tests/integration/oracle/test_oracle_gate.py` proves every golden task is satisfiable. For each task
id in the yaml, a scripted provider (`tests/integration/oracle/scripted_provider.py`) replays a
known-correct sequence of tool calls and answers through the real `observe_turn` and scoring, against
a freshly seeded tenant in DynamoDB Local. Every applicable tier must pass and every script must be
fully consumed. A negative control (one bare supervisor answer per task) must fail the supervisor tier.
A task with no script fails the gate, and so does a script for an unknown task. The scripted provider
is test-only: no `src/` module and no harness flag can select it.

Run it with DynamoDB Local up:

```bash
CRM_REQUIRE_DYNAMODB_LOCAL=1 .venv/bin/python -m pytest tests/integration/oracle/test_oracle_gate.py
```

**Adding a task.** Add the task to `src/eval/golden_tasks.yaml`, then add its script to the
`SCRIPTS` mapping of the matching `tests/integration/oracle/scripts_*.py` module (or a new
`scripts_<group>.py`; the gate discovers every one). A script is a tuple of `Call(role, tool, args)`
and `Answer(role, text)` steps (`tests/integration/oracle/_contract.py`). Roles are `supervisor`,
`research`, `mutation`, `analytics` and `composer`, and ids are tenant-relative (`opp-0000`); the
provider composes them into the tenant. A task may carry more than one script, and each must pass.
Use a fixed UUID for every `transaction_key`.

## `forbidden_outcomes` label disposition

Each golden task may declare `forbidden_outcomes` labels (for example `wrong_contact_mutated`,
`silent_truncation`). Every declared label is either:

- **structurally falsifiable**: a member of `_STRUCTURALLY_FALSIFIABLE_FORBIDDEN_OUTCOMES` in
  `src/eval/scoring.py`, checked against a real signal `score_task` computes; or
- **content-only-unscored**: a member of `_CONTENT_ONLY_FORBIDDEN_OUTCOMES`, a property of prose the
  harness cannot check without a judge model.

There is no third category. A label in neither registry fails:

```bash
.venv/bin/python -m pytest tests/unit/test_forbidden_outcomes_falsifiable.py::test_every_declared_forbidden_outcomes_label_is_dispositioned
```

`wrong_contact_mutated` and `silent_update_of_departed_contact` derive from failed `contacts`
`must_not_change` results, so they are falsifiable only where a pre-turn baseline is captured.

## Prompt caching

Both family adapters apply one policy, defined in `src/providers/prompt_caching.py`:

- **Implicit caching stays on.** Both families keep the platform's implicit caching. The OpenAI
  adapter sends `prompt_cache_options {mode: implicit, ttl: 30m}` on every Responses request, the
  default mode, which adds an automatic breakpoint on the latest message and also honours explicit
  breakpoints.
- **A breakpoint at the end of the stable prefix.** The stable prefix is the tool definitions
  plus the leading run of system messages, up to but not including the first message marked
  `cache_scope: volatile`. The supervisor's page briefing carries that marker, so it follows the
  breakpoint. The marker is never sent to either API. The Anthropic adapter puts
  `cache_control {type: ephemeral}` on that system block. The OpenAI adapter puts
  `prompt_cache_breakpoint {mode: explicit}` on that system message's `input_text` block. The tool
  tier sends no system message, so its requests carry no explicit breakpoint.
- **A point on the latest message, matched across families.** The OpenAI platform's implicit
  caching already places an automatic breakpoint on the latest message. The Anthropic adapter
  matches it: it also puts `cache_control {type: ephemeral}` on the last content block of the
  latest non-system message, so a later turn can read the conversation history cached by an earlier
  one, not only the stable prefix. This applies at every tier, including the tool tier, which
  carries no stable-prefix point.
- **At most four points on an Anthropic request.** A Messages API request carries no more than four
  `cache_control` points. When there are more candidates, the latest-message point is kept first,
  then the stable-prefix point, then the most recent others; the oldest are dropped.
- **No point below the minimum.** The adapter omits a point when the prefix up to and including it
  is too short to reach the model's `prompt_cache_min_checkpoint_tokens`. For the latest-message
  point that prefix is the tool definitions plus every message through the latest, including tool
  calls and results. It estimates size at two characters per token, so it omits a point only when
  the prefix cannot reach the minimum.

Each arm records its family's method as `provenance.prompt_caching`, the text of `CACHING` in
`src/providers/prompt_caching.py`, so a result record states how its requests asked to be cached.

The platform sets these per family. The last two differ between arms:

| | Anthropic | OpenAI |
|---|---|---|
| Implicit caching | always on, best effort; no request field turns it off | on in the `implicit` mode the adapter sends; `explicit` mode would turn it off |
| Minimum cacheable prefix | 512 tokens | 1,024 tokens |
| TTL | 5 minutes or 1 hour on the platform; the adapter uses the 5-minute default | 30 minutes, the only value |

`prompt_cache_min_checkpoint_tokens` in `config/model_capabilities.yaml`, per model. The Home tier
column is each model's home tier; `anthropic.claude-sonnet-5-5` is the default at both the
supervisor and subagent tiers.

| Model | Home tier | `prompt_cache_min_checkpoint_tokens` |
|---|---|---|
| `anthropic.claude-opus-5-5` | supervisor | 512 |
| `anthropic.claude-sonnet-5-5` | subagent | 512 |
| `anthropic.claude-haiku-5-5` | tool | 512 |
| `openai.gpt-6-astra` | supervisor | 1024 |
| `openai.gpt-6.1-sol` | supervisor | 1024 |
| `openai.gpt-6.1-terra` | subagent | 1024 |
| `openai.gpt-6-luna` | tool | 1024 |

The file's `provenance.sources` lists the AWS pages these values come from.

An identical stable prefix of 512 to 1,023 tokens can therefore cache on an Anthropic model and not
on an OpenAI one, purely because of its length. Every result record carries the per-model threshold
beside its observed cache reads and writes, and `matrix.md` reports cache-driven cost separately from
model-driven cost. A cache read is a billing and latency property; no quality inference is drawn
from it. Each family's caching mechanism is a member of the prompt bundle, so a change to it changes
`prompt_bundle_sha256`.

## Limitations

- **n=1 per arm by default.** Every figure is a single observation; no difference between arms is
  called meaningful. `--repeats` raises the repetition count, and task completion over repeats
  reports the strict-majority figure.
- **One dataset seed** per sweep.
- **One prompt bundle per instrument.** Sweeps with different `prompt_bundle_sha256` values sent
  different prompts, tool descriptions or caching requests (see What the models are sent).
- **Sweeps before record shape 3 are a different instrument.** Shape 3 coincides with four changes
  that move figures: the 55 planted activities on `opp-0000`, the tool-output bound on every path,
  leaf calls charged as they are dispatched (with refused leaves counted at ToolCall), and the
  resized `gt-04`. Earlier sweeps' correctness, truncation, budget-exhaustion and cost figures are
  not comparable with later ones figure for figure, and their records are refused rather than read.
- **The CLI `--live` sweep does not exercise the deployed AgentCore Runtime**
  (`live-aws-lambda-dispatch`). A state-machine sweep runs each task on the Runtime
  (`agentcore-runtime-eval-turn`).
- **Report quality is not measured** (see Correctness figures).
