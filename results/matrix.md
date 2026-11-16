# Model Evaluation Matrix

## Tier comparison summary

Per-tier figures for each arm, one column per arm in run order; the notes below the tables define each row.

### Supervisor

| Metric | anthropic | openai |
| --- | --- | --- |
| Model | anthropic.claude-sonnet-5-5 | openai.gpt-6.1-sol |
| Effort | medium | medium |
| Task completion rate | 0.900000 (108/120) | 0.991667 (119/120) |
| Task completion over repeats | 0.916667 (22/24, majority of 5) | 1.000000 (24/24, majority of 5) |
| Task accuracy | 0.975463 | 0.999074 |
| Cost per completed task | 0.023736 | 0.014555 |
| P50 latency per completed task (ms) | 3800.814666 (n=108) | 3696.228625 (n=119) |
| P95 latency per completed task (ms) | 15170.417917 (n=108) | 20859.479959 (n=119) |
| Model invocations | 290 | 268 |
| Long-context invocations | 0 | 0 |
| Total input tokens | 2482245 | 1579162 |
| Cache read tokens | 1815158 | 1086832 |
| Cache write tokens | 666267 | 491794 |
| Uncached input tokens | 820 | 536 |
| Total output tokens | 71463 | 39285 |
| Cost per applicable task | 0.021362 | 0.014434 |
| Self-corrected retries | 0 | 0 |
| Budget-exhausted tasks | 0 | 0 |
| Truncated tool calls | 20 | 16 |
| Truncated tasks | 20 | 16 |

### Subagent

| Metric | anthropic | openai |
| --- | --- | --- |
| Model | anthropic.claude-sonnet-5-5 | openai.gpt-6.1-terra |
| Effort | low | low |
| Task completion rate | 1.000000 (115/115) | 0.991304 (114/115) |
| Task completion over repeats | 1.000000 (23/23, majority of 5) | 1.000000 (23/23, majority of 5) |
| Task accuracy | 1.000000 | 0.991304 |
| Cost per completed task | 0.004743 | 0.007421 |
| P50 latency per completed task (ms) | 4295.231875 (n=50) | 2629.128000 (n=65) |
| P95 latency per completed task (ms) | 6844.754083 (n=50) | 10918.713583 (n=65) |
| Model invocations | 105 | 193 |
| Long-context invocations | 0 | 0 |
| Total input tokens | 781735 | 1597649 |
| Cache read tokens | 691126 | 1076668 |
| Cache write tokens | 90299 | 520595 |
| Uncached input tokens | 310 | 386 |
| Total output tokens | 24997 | 28195 |
| Cost per applicable task | 0.004743 | 0.007356 |
| Self-corrected retries | 0 | 0 |
| Budget-exhausted tasks | 0 | 0 |
| Truncated tool calls | not applicable | not applicable |
| Truncated tasks | not applicable | not applicable |

### ToolCall

| Metric | anthropic | openai |
| --- | --- | --- |
| Model | anthropic.claude-haiku-5-5 | openai.gpt-6-luna |
| Effort | low | low |
| Task completion rate | 1.000000 (95/95) | 0.989474 (94/95) |
| Task completion over repeats | 1.000000 (19/19, majority of 5) | 1.000000 (19/19, majority of 5) |
| Task accuracy | 1.000000 | 0.989474 |
| Cost per completed task | 0.000169 | 0.000142 |
| P50 latency per completed task (ms) | 2150.232458 (n=50) | 1233.549624 (n=61) |
| P95 latency per completed task (ms) | 4397.095708 (n=50) | 8126.829000 (n=61) |
| Model invocations | 67 | 194 |
| Long-context invocations | 0 | 0 |
| Total input tokens | 63158 | 52305 |
| Cache read tokens | 0 | 0 |
| Cache write tokens | 62890 | 0 |
| Uncached input tokens | 268 | 52305 |
| Total output tokens | 16394 | 16250 |
| Cost per applicable task | 0.000169 | 0.000141 |
| Self-corrected retries | not applicable | not applicable |
| Budget-exhausted tasks | 0 | 0 |
| Truncated tool calls | 0 | 21 |
| Truncated tasks | 0 | 11 |

Notes:

- Model lists the tier's configured model and every model that spent tokens at the tier; Effort lists the efforts the tier's turns ran at, with unsupported for a model that takes no effort setting. Task completion rate is rate (met/applicable) over the arm's tasks applicable at the tier, where n is the applicable task count: at Supervisor a task's correctness is 1.0, at Subagent the observed delegation outcome equals the expected one, and at ToolCall an expected tool was called with schema-valid arguments. Task accuracy is the arm's mean_correctness at Supervisor and the completion rate at Subagent and ToolCall. Task completion over repeats is rate (completed/applicable) over the arm's tasks applicable at the tier in any repeat, where a task counts when its tier expectation was met in a strict majority of its applicable repeats, and N is the per-task repeat count. Cost per completed task divides the tier's total cost by its completed task count, so spend on tasks that did not complete is included; Cost per applicable task divides the same total by the tier's applicable task count (n of Task completion rate), so the two rows are equal when every applicable task completed, and it reads — when no task was applicable. Latency per completed task sums the tier's measured call latencies within each completed task and takes the nearest-rank percentile over those tasks; a completed task with no measured call at the tier contributes no sample, and n counts the contributing tasks. Model invocations sums the tier's included invocations; Long-context invocations counts those priced at the model's long-context rates, and reads — for a run recorded before that count existed. Self-corrected retries counts the tier's tool calls whose tool_failure result was followed, later in the same task, by a call to the same tool from the same role that returned data or no_match; such a call does not set the task's delegation outcome. ToolCall makes one call per invocation, so that row is not applicable there. Budget-exhausted tasks counts the task runs whose turn budget ran out at the tier. Truncated tool calls counts the tier's tool calls whose result was truncated, the Supervisor's own direct calls at Supervisor and every call made inside a delegation at ToolCall; Truncated tasks counts the task runs with at least one such call. Subagent makes no tool call itself, so both truncation rows are not applicable there.
- Columns are the sweep's arms in run order, not sorted by any result, and no difference or ratio between columns is computed.
- Effort labels are not equivalent across families: each family declares its own effort scale (config/model_capabilities.yaml, effort_scale_comparability); see `effort` under "Reading these figures".
- Total input tokens sums, per model call, the whole prompt the call sent, counted the same way for both families: the provider's reported input plus cache read and cache write tokens for Anthropic, whose reported input excludes them, and the reported input for OpenAI, whose reported input includes them, or that call's cache read plus cache write tokens where they exceed it. Cache read tokens and Cache write tokens sum the tier's tokens served from and written to the prompt cache. Uncached input tokens is Total input tokens minus Cache read tokens minus Cache write tokens, so in every column Total input tokens = Cache read tokens + Cache write tokens + Uncached input tokens exactly. Total output tokens sums the tier's output tokens, reasoning tokens included. The per-model tables below keep each family's own reported input count (token_accounting_disclosure, src/eval/matrix.py); see `input_tokens` comparability under "Reading these figures".

Measured dimensions per model arm — direct measurements only; no reference threshold or conclusion is computed or shown here.

## Reading these figures

Each figure below names one narrow measured signal. Its boundary is stated here so the artifact cannot be read past what it supports. Mechanism and evidence: src/eval/matrix.py.

- `mean_correctness` — The mean fraction of machine-checkable assertions satisfied over the tasks that were actually scored. Not a measure of end-to-end task success: an assertion naming a record that does not exist under the tenant an arm ran against lowers this figure whatever the turn did. Most of what it averages are negative guards a turn that does nothing at all satisfies, so its dynamic range is compressed well above zero and the figure is not a task-achievement rate; each arm's own positive-versus-guard composition, including the positive-assertion satisfaction rate, is stated with its aggregate figures.
- `bad_write_rate` — Reports only what a captured pre-turn baseline makes observable: a `must_not_change` assertion with no baseline is reported unscored and contributes nothing. Each arm's own basis is stated with its aggregate figures.
- `tool_call_correct_rate` — Reports that each scored turn issued a tool call which dispatched against an in-scope target. It is derived from exactly three conditions — a turn that issued no tool call at all, a proposal submitted on a task whose proposal limit is zero, and a proposal targeting a record other than the expected one — and from nothing else. It therefore does not report whether the turn chose the most apt tool for the task; where a task names the retrieval its answer requires, an `expected_reads` assertion checks that at least one call to a named tool reached the named record, and contributes to `mean_correctness` and deliberately not to this figure. An entry whose `target_id` is null checks only that one of the named tools was called at all, never that any particular record was reached, because the tools it names take no record id. Read to its exact boundary: it is satisfied incidentally by a turn that calls the named tool on the named record among many other calls, it says nothing about what the turn then reported, and it is therefore a necessary condition for an apt retrieval and never a sufficient one (src/eval/scoring.py, `_check_expected_reads`).
- report quality — Not measured anywhere in this project, by any figure in this artifact. Two tasks score retrieval in place of report quality: grading a multi-part prose report needs a judge model, which this project forbids, so their assertions check that the turn read the records the answer requires and nothing checks what the turn then wrote. A turn that reads exactly the right records and then reports them incompletely, or wrongly, still satisfies those assertions and still counts toward `mean_correctness`. The `lookup` tasks' positive signal is retrieval on the same terms, and for two of the four it is weaker still: their `expected_reads` entry carries a null `target_id`, so the satisfied assertion evidences only that the named whole-book tool was called at all, not that any particular record was read. The narrowing was authorised deliberately and each narrowed task's own `notes` in golden_tasks.yaml restate the superseded reasoning at length. The tasks narrowed to retrieval are: `gt-04-investigation-turn-budget-partial`, `gt-05-investigation-large-tool-result`.
- retrieval narrowing — A task narrowed to retrieval names the specific retrieval its assertions check, and names only the retrievals that expose an observable target id. A turn that answers such a task by an equally apt retrieval which exposes no target id therefore satisfies none of that task's positive assertions, for a reason unrelated to what the model did. The deduction that conservatism makes to `mean_correctness` is quantified per arm with that arm's aggregate figures, as an upper bound derived from the arm's own per-task records, so a reader of this artifact alone can size it against the figures it appears beside.
- Retrieval-narrowing magnitude: the largest deduction any one arm's retrieval-narrowed tasks account for is 0.000000 of that arm's `mean_correctness`, which is smaller than the largest absolute difference between any two arms' `mean_correctness` in this sweep (0.023611). Which arm sits on which side of that difference is deliberately not computed or stated anywhere in this artifact; the comparison is published because a disclosed measurement conservatism of this size bounds how much the difference between arms can be read as a property of the models (src/eval/golden_tasks.yaml).
- `effort` (provenance) — The reasoning-effort string sent to each arm's supervisor tier, pinned identically across arms so the sweep is reproducible; the subagent and tool tiers run their config/models.yaml tier effort. It is not a claim that the arms were given equivalent reasoning budgets: each family declares its own effort scale with its own rung count and its own default, so an identical label sits at a different position on each family's scale. The declared scales, and this non-equivalence, are recorded in config/model_capabilities.yaml under `effort_scale_comparability.compared_models`, per tier. Sweeps recorded before tier efforts were configured ran the subagent and tool tiers at each model's own default effort (the Anthropic subagent at `high`), so their subagent and tool tiers are not effort-comparable with this artifact's.
- `long_context_invocation_count` — counts the model invocations priced at the invoked model's long-context rates, i.e. a call whose prompt (input plus cache-read plus cache-write tokens) exceeded the model's long-context threshold in config/pricing.yaml; such a call's every token class is priced at those rates. It is a count of calls, not of tokens, and a model with no long-context tier never contributes. A run recorded before the count existed shows —, never 0.
- arm totals — an arm's Usage and Cost (model_metrics) covers only the supervisor-tier calls to the arm's model; the arm totals (the arm cost and arm_long_context_invocation_count) cover every call the arm made at every tier, so the arm cost equals the sum of the three tiers' costs.
- `input_tokens` comparability — Each arm's `input_tokens` is the count its own provider family reported, and the families report cached prompt reads in differently-shaped usage objects: one as a sibling of `input_tokens`, the other nested inside its breakdown. Whether a family's `input_tokens` already contains the `cache_read_tokens` printed beside it is therefore a per-family question, and this project declares it per family with the evidence behind the declaration rather than assuming the two figures count the same thing. Two consequences, both exact: the arms' `input_tokens` are not established to be like-for-like totals and should not be differenced or ratioed as though they were; and while any family's accounting is declared unconfirmed, pricing refuses to produce a publishable currency figure for it at all, so no cost comparison can rest on the ambiguity. No token figure in this artifact is adjusted by this disclosure — each is the count that was reported. (src/crm/metering.py)
  - `anthropic`: cached reads read from `usage.cache_read_input_tokens (Messages API message_start)`; accounting priced under: `exclusive`; direction the evidence points: `exclusive`. Exclusive (disjoint): inputTokens excludes both cacheReadInputTokens and cacheWriteInputTokens; the total input is their sum. AWS's own Bedrock prompt-caching walkthrough shows a cache-read response as {"inputTokens": 28, "outputTokens": 294, "cacheWriteInputTokens": 0, "cacheReadInputTokens": 1898} and states "Only 28 tokens (the question itself) were processed as standard input" while the 1,898 reused tokens are reported separately in cacheReadInputTokens — so the cached count is not inside inputTokens. Source: https://aws.amazon.com/blogs/machine-learning/optimizing-cost-and-latency-with-amazon-bedrock-prompt-caching/ (verified 2026-09-28). The Messages API message_start path the adapter reads carries the same disjoint cache_read_input_tokens / cache_creation_input_tokens siblings.
  - `openai`: cached reads read from `usage.input_tokens_details.cached_tokens (Responses API response.completed)`; accounting priced under: `inclusive`; direction the evidence points: `inclusive`. Inclusive (subset): input_tokens is the full input count and both the cached reads and the cache writes are part of it, not added on top. AWS's Bedrock explicit-caching documentation for the OpenAI models states verbatim "input_tokens = cached_tokens + cache_write_tokens + non-cached remainder" and "Because the cached and written counts are part of input_tokens rather than added on top of it, a token served from the cache is counted once and billed once at the cache-read rate. You are never charged twice for the same token." Its worked example confirms the arithmetic (input_tokens 3682 = cached 0 + cache_write 3626 + new 56). Source: https://aws.amazon.com/blogs/machine-learning/introducing-explicit-prompt-caching-for-openai-gpt-5-6-models-on-amazon-bedrock/ (verified 2026-09-28). So both cached_tokens and cache_write_tokens must be subtracted from input_tokens to obtain the tokens billed at the standard input rate. CAVEAT: that AWS source describes the GPT-5.6 models; the OpenAI models in the matrix use the same Responses API usage shape (input_tokens with input_tokens_details.cached_tokens), and no vendor statement of this accounting specific to them was found, so the reading is carried forward by shape. The first --live sweep that records a non-zero cached read should be reconciled against the actual Bedrock bill to confirm it holds for them.

## anthropic.claude-sonnet-5-5 (live-mode run)

### Provenance

| field | value |
| --- | --- |
| mode | live |
| timestamp | 2026-11-15T20:29:19.713862+00:00 |
| model_id | anthropic.claude-sonnet-5-5 |
| effort | medium |
| dataset_seed | 20260101 |
| runtime_identifier | live-aws-lambda-dispatch |
| price_table_version | 1.3.0 |
| answer_style_sha256 | 953efa6d0d26da1fb09cf2cdf625c78f0bd8f7d04da7652e581e623edea67f47 |
| prompt_bundle_sha256 | aaea4152ed4a88dd997bbce0211ebc998f61d01c363faed83cda3aee6194ba7e |
| prompt_caching | explicit: cache_control {type: ephemeral} (5-minute TTL) on the last stable system block and on the last content block of the latest message, at most 4 points, each omitted when its cumulative prefix is below prompt_cache_min_checkpoint_tokens; beside the platform's implicit caching, which no request field turns off |

### Usage and Cost

Supervisor tier only: these figures cover the arm's supervisor-tier calls to anthropic.claude-sonnet-5-5.

| field | value |
| --- | --- |
| model_id | anthropic.claude-sonnet-5-5 |
| included_invocation_count | 290 |
| long_context_invocation_count | 0 |
| input_tokens | 820 |
| output_tokens | 71463 |
| cache_write_tokens | 666267 |
| cache_read_tokens | 1815158 |
| cache_min_checkpoint_tokens | 512 |
| latency_measured | yes |
| latency_percentile_method | nearest-rank |
| task_latency_ms_p50 | 8620.837667 |
| task_latency_ms_p95 | 22294.123375 |
| cost_withheld_provisional | no |
| cost_input | 0.001640 |
| cost_output | 0.714630 |
| cost_cache_write | 1.665668 |
| cost_cache_read | 0.181516 |
| cost_excluding_cache | 0.716270 |
| cost_cache_total | 1.847183 |
| cost_total | 2.563453 |
| price_table_version | 1.3.0 |

All tiers (every call the arm made):

| field | value |
| --- | --- |
| arm_cost_total | 3.124988 |
| arm_long_context_invocation_count | 0 |

Call latency by tier (ms):

| tier | p50 | p95 | count |
| --- | --- | --- | --- |
| supervisor | 1731.026416 | 7774.722125 | 290 |
| subagent | 1944.460500 | 3631.187791 | 105 |
| tool | 1881.982583 | 2646.677500 | 67 |

Per tier / role / model:

| tier | role | model_id | included_invocation_count | long_context_invocation_count | input_tokens | output_tokens | cache_read_tokens | cache_write_tokens | call_latency_measured | call_latency_ms_p50 | call_latency_ms_p95 | cost_withheld_provisional | cost_input | cost_output | cost_cache_read | cost_cache_write | cost_excluding_cache | cost_cache_total | cost_total | price_table_version |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| subagent | mutation | anthropic.claude-sonnet-5-5 | 105 | 0 | 310 | 24997 | 691126 | 90299 | yes | 1944.460500 | 3631.187791 | no | 0.000620 | 0.249970 | 0.069113 | 0.225747 | 0.250590 | 0.294860 | 0.545450 | 1.3.0 |
| supervisor | supervisor | anthropic.claude-sonnet-5-5 | 290 | 0 | 820 | 71463 | 1815158 | 666267 | yes | 1731.026416 | 7774.722125 | no | 0.001640 | 0.714630 | 0.181516 | 1.665668 | 0.716270 | 1.847183 | 2.563453 | 1.3.0 |
| tool | mutation | anthropic.claude-haiku-5-5 | 67 | 0 | 268 | 16394 | 0 | 62890 | yes | 1881.982583 | 2646.677500 | no | 0.000027 | 0.008197 | 0.000000 | 0.007861 | 0.008224 | 0.007861 | 0.016085 | 1.3.0 |

Per-tier summary:

| tier | input_tokens | output_tokens | cost | latency_p50_ms | latency_p95_ms | success (met/applicable) | success_rate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| supervisor | 820 | 71463 | 2.563453 | 1731.026416 | 7774.722125 | 108/120 | 0.900000 |
| subagent | 310 | 24997 | 0.545450 | 1944.460500 | 3631.187791 | 115/115 | 1.000000 |
| tool | 268 | 16394 | 0.016085 | 1881.982583 | 2646.677500 | 95/95 | 1.000000 |

Per tool:

| tool_name | role | count | call_latency_ms_p50 | call_latency_ms_p95 | truncated_count | outcome_data | outcome_no_match | outcome_denied | outcome_tool_failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| create_note | mutation | 10 | 310.324792 | 1967.516375 | 0 | 10 | 0 | 0 | 0 |
| get_account | supervisor | 20 | 268.871292 | 1149.386333 | 0 | 20 | 0 | 0 | 0 |
| get_contact | supervisor | 25 | 258.061959 | 676.306125 | 0 | 25 | 0 | 0 | 0 |
| get_opportunity | supervisor | 77 | 302.917583 | 427.858708 | 15 | 67 | 5 | 5 | 0 |
| list_activities | supervisor | 33 | 302.513708 | 456.217166 | 5 | 28 | 0 | 0 | 5 |
| list_proposals | supervisor | 5 | 342.898417 | 1900.332709 | 0 | 5 | 0 | 0 | 0 |
| log_activity | mutation | 10 | 316.870667 | 2107.360917 | 0 | 10 | 0 | 0 | 0 |
| pipeline_aggregate | supervisor | 5 | 582.885750 | 2305.109209 | 0 | 5 | 0 | 0 | 0 |
| search_accounts | supervisor | 5 | 366.768709 | 1969.900708 | 0 | 5 | 0 | 0 | 0 |
| search_opportunities | supervisor | 20 | 246.852084 | 289.532708 | 0 | 10 | 0 | 10 | 0 |
| submit_proposal | mutation | 37 | 307.957333 | 466.227083 | 0 | 37 | 0 | 0 | 0 |
| update_account | mutation | 5 | 346.125333 | 2167.570500 | 0 | 5 | 0 | 0 | 0 |
| update_contact | mutation | 5 | 308.749166 | 1979.124666 | 0 | 5 | 0 | 0 | 0 |

Excluded invocations: 1 total (cold_start: 0, retry: 1, throttle: 0)

### Aggregate Score

| field | value |
| --- | --- |
| scored_task_count | 120 |
| unscored_task_count | 0 |
| mean_correctness | 0.975463 |
| bad_write_rate | 0.000000 |
| tool_call_correct_rate | 1.000000 |

`bad_write_rate` basis: all 255 declared `must_not_change` assertion(s) in this arm had a captured pre-turn baseline, so the figure is a measurement over them (src/eval/matrix.py).

`mean_correctness` composition: negative guards a do-nothing turn satisfies are 0.8153 of the assertion mass this figure averages — the mean over the 120 scored tasks of each task's own guard share, weighted exactly as `mean_correctness` itself is, rather than the ratio pooled over assertions (pooled totals, for context and not the basis of the figure just quoted: 692 guard assertions and 150 positive ones across those tasks). So the figure is not a task-achievement rate. Taken alone, 143 of 150 positive assertions were satisfied (0.9533); the guards alone were satisfied at 0.9682 (src/eval/matrix.py).

Retrieval-narrowed task deduction: the assertions this arm did not satisfy on the tasks narrowed to retrieval account for at most 0.000000 of its `mean_correctness` (`gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000), an upper bound over the 120 scored tasks derived from this arm's own per-task records. The figure itself is unchanged by this disclosure (src/eval/golden_tasks.yaml).

### Per-Task Results

| task_id | scoring_mode | correctness | unscored_reason | bad_write | tool_call_correct | truncated | budget_exhausted |
| --- | --- | --- | --- | --- | --- | --- | --- |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | yes | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | yes | no |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | yes | no |
| gt-06-enrichment-create-note | machine | 1.000000 | — | no | yes | no | no |
| gt-07-enrichment-log-activity-last-touch | machine | 1.000000 | — | no | yes | no | no |
| gt-08-enrichment-stage-advance-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-09-enrichment-value-and-close-date-proposal | machine | 0.800000 | — | no | yes | no | no |
| gt-10-enrichment-update-account-fields | machine | 1.000000 | — | no | yes | no | no |
| gt-11-denial-out-of-book-target | machine | 1.000000 | — | no | yes | no | no |
| gt-12-denial-nonexistent-target | machine | 1.000000 | — | no | yes | no | no |
| gt-13-denial-cross-account-comparison | machine | 1.000000 | — | no | yes | no | no |
| gt-14-denial-departed-contact-update | machine | 1.000000 | — | no | yes | no | no |
| gt-15-denial-service-principal-no-implicit-visibility | machine | 1.000000 | — | no | yes | no | no |
| gt-16-denial-tool-failure-not-conflated | machine | 1.000000 | — | no | yes | no | no |
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | yes | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 0.700000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | yes | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | yes | no |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | yes | no |
| gt-06-enrichment-create-note | machine | 1.000000 | — | no | yes | no | no |
| gt-07-enrichment-log-activity-last-touch | machine | 1.000000 | — | no | yes | no | no |
| gt-08-enrichment-stage-advance-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-09-enrichment-value-and-close-date-proposal | machine | 0.800000 | — | no | yes | no | no |
| gt-10-enrichment-update-account-fields | machine | 1.000000 | — | no | yes | no | no |
| gt-11-denial-out-of-book-target | machine | 1.000000 | — | no | yes | no | no |
| gt-12-denial-nonexistent-target | machine | 1.000000 | — | no | yes | no | no |
| gt-13-denial-cross-account-comparison | machine | 1.000000 | — | no | yes | no | no |
| gt-14-denial-departed-contact-update | machine | 1.000000 | — | no | yes | no | no |
| gt-15-denial-service-principal-no-implicit-visibility | machine | 1.000000 | — | no | yes | no | no |
| gt-16-denial-tool-failure-not-conflated | machine | 1.000000 | — | no | yes | no | no |
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | yes | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 0.700000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | yes | no |
| gt-02-investigation-pd02-stale-forecast | machine | 0.777778 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | yes | no |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | yes | no |
| gt-06-enrichment-create-note | machine | 1.000000 | — | no | yes | no | no |
| gt-07-enrichment-log-activity-last-touch | machine | 1.000000 | — | no | yes | no | no |
| gt-08-enrichment-stage-advance-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-09-enrichment-value-and-close-date-proposal | machine | 0.800000 | — | no | yes | no | no |
| gt-10-enrichment-update-account-fields | machine | 1.000000 | — | no | yes | no | no |
| gt-11-denial-out-of-book-target | machine | 1.000000 | — | no | yes | no | no |
| gt-12-denial-nonexistent-target | machine | 1.000000 | — | no | yes | no | no |
| gt-13-denial-cross-account-comparison | machine | 1.000000 | — | no | yes | no | no |
| gt-14-denial-departed-contact-update | machine | 1.000000 | — | no | yes | no | no |
| gt-15-denial-service-principal-no-implicit-visibility | machine | 1.000000 | — | no | yes | no | no |
| gt-16-denial-tool-failure-not-conflated | machine | 1.000000 | — | no | yes | no | no |
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | yes | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 0.700000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | yes | no |
| gt-02-investigation-pd02-stale-forecast | machine | 0.777778 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | yes | no |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | yes | no |
| gt-06-enrichment-create-note | machine | 1.000000 | — | no | yes | no | no |
| gt-07-enrichment-log-activity-last-touch | machine | 1.000000 | — | no | yes | no | no |
| gt-08-enrichment-stage-advance-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-09-enrichment-value-and-close-date-proposal | machine | 0.800000 | — | no | yes | no | no |
| gt-10-enrichment-update-account-fields | machine | 1.000000 | — | no | yes | no | no |
| gt-11-denial-out-of-book-target | machine | 1.000000 | — | no | yes | no | no |
| gt-12-denial-nonexistent-target | machine | 1.000000 | — | no | yes | no | no |
| gt-13-denial-cross-account-comparison | machine | 1.000000 | — | no | yes | no | no |
| gt-14-denial-departed-contact-update | machine | 1.000000 | — | no | yes | no | no |
| gt-15-denial-service-principal-no-implicit-visibility | machine | 1.000000 | — | no | yes | no | no |
| gt-16-denial-tool-failure-not-conflated | machine | 1.000000 | — | no | yes | no | no |
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | yes | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 0.700000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | yes | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | yes | no |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | yes | no |
| gt-06-enrichment-create-note | machine | 1.000000 | — | no | yes | no | no |
| gt-07-enrichment-log-activity-last-touch | machine | 1.000000 | — | no | yes | no | no |
| gt-08-enrichment-stage-advance-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-09-enrichment-value-and-close-date-proposal | machine | 0.800000 | — | no | yes | no | no |
| gt-10-enrichment-update-account-fields | machine | 1.000000 | — | no | yes | no | no |
| gt-11-denial-out-of-book-target | machine | 1.000000 | — | no | yes | no | no |
| gt-12-denial-nonexistent-target | machine | 1.000000 | — | no | yes | no | no |
| gt-13-denial-cross-account-comparison | machine | 1.000000 | — | no | yes | no | no |
| gt-14-denial-departed-contact-update | machine | 1.000000 | — | no | yes | no | no |
| gt-15-denial-service-principal-no-implicit-visibility | machine | 1.000000 | — | no | yes | no | no |
| gt-16-denial-tool-failure-not-conflated | machine | 1.000000 | — | no | yes | no | no |
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | yes | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 0.700000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |

Per-task correctness spread across repeats:

| task_id | repeats | correctness_min | correctness_mean | correctness_max | success_fraction |
| --- | --- | --- | --- | --- | --- |
| gt-01-investigation-pd01-successor | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-02-investigation-pd02-stale-forecast | 5 | 0.777778 | 0.911111 | 1.000000 | 0.600000 |
| gt-03-investigation-pd03-scoped-lookup | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-04-investigation-turn-budget-partial | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-05-investigation-large-tool-result | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-06-enrichment-create-note | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-07-enrichment-log-activity-last-touch | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-08-enrichment-stage-advance-proposal | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-09-enrichment-value-and-close-date-proposal | 5 | 0.800000 | 0.800000 | 0.800000 | 0.000000 |
| gt-10-enrichment-update-account-fields | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-11-denial-out-of-book-target | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-12-denial-nonexistent-target | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-13-denial-cross-account-comparison | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-14-denial-departed-contact-update | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-15-denial-service-principal-no-implicit-visibility | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-16-denial-tool-failure-not-conflated | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-17-lookup-opportunity-detail | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-18-lookup-account-contacts | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-19-lookup-pipeline-aggregate | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-20-lookup-proposal-queue | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-21-enrichment-pd04-multi-field-proposal | 5 | 0.700000 | 0.700000 | 0.700000 | 0.000000 |
| gt-22-investigation-pd05-report-only | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-23-investigation-pd06-ambiguous-contact | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-24-enrichment-pd07-multi-step-write | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |

## openai.gpt-6.1-sol (live-mode run)

### Provenance

| field | value |
| --- | --- |
| mode | live |
| timestamp | 2026-11-15T20:50:38.391703+00:00 |
| model_id | openai.gpt-6.1-sol |
| effort | medium |
| dataset_seed | 20260101 |
| runtime_identifier | live-aws-lambda-dispatch |
| price_table_version | 1.3.0 |
| answer_style_sha256 | 953efa6d0d26da1fb09cf2cdf625c78f0bd8f7d04da7652e581e623edea67f47 |
| prompt_bundle_sha256 | aaea4152ed4a88dd997bbce0211ebc998f61d01c363faed83cda3aee6194ba7e |
| prompt_caching | explicit: prompt_cache_breakpoint {mode: explicit} on the last stable system input_text block, beside implicit caching (prompt_cache_options {mode: implicit, ttl: 30m}, an automatic breakpoint on the latest message); minimum prefix from prompt_cache_min_checkpoint_tokens |

### Usage and Cost

Supervisor tier only: these figures cover the arm's supervisor-tier calls to openai.gpt-6.1-sol.

| field | value |
| --- | --- |
| model_id | openai.gpt-6.1-sol |
| included_invocation_count | 268 |
| long_context_invocation_count | 0 |
| input_tokens | 1579162 |
| output_tokens | 39285 |
| cache_write_tokens | 491794 |
| cache_read_tokens | 1086832 |
| cache_min_checkpoint_tokens | 1024 |
| latency_measured | yes |
| latency_percentile_method | nearest-rank |
| task_latency_ms_p50 | 6716.815625 |
| task_latency_ms_p95 | 36927.114417 |
| cost_withheld_provisional | no |
| cost_input | 0.001072 |
| cost_output | 0.392850 |
| cost_cache_write | 1.229485 |
| cost_cache_read | 0.108683 |
| cost_excluding_cache | 0.393922 |
| cost_cache_total | 1.338168 |
| cost_total | 1.732090 |
| price_table_version | 1.3.0 |

All tiers (every call the arm made):

| field | value |
| --- | --- |
| arm_cost_total | 2.591384 |
| arm_long_context_invocation_count | 0 |

Call latency by tier (ms):

| tier | p50 | p95 | count |
| --- | --- | --- | --- |
| supervisor | 1829.730625 | 5089.577459 | 268 |
| subagent | 1163.972458 | 3181.016541 | 193 |
| tool | 620.995708 | 1462.112541 | 194 |

Per tier / role / model:

| tier | role | model_id | included_invocation_count | long_context_invocation_count | input_tokens | output_tokens | cache_read_tokens | cache_write_tokens | call_latency_measured | call_latency_ms_p50 | call_latency_ms_p95 | cost_withheld_provisional | cost_input | cost_output | cost_cache_read | cost_cache_write | cost_excluding_cache | cost_cache_total | cost_total | price_table_version |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| subagent | analytics | openai.gpt-6.1-terra | 10 | 0 | 22008 | 1188 | 5384 | 16604 | yes | 1107.749625 | 1588.076166 | no | 0.000020 | 0.005940 | 0.000269 | 0.020755 | 0.005960 | 0.021024 | 0.026984 | 1.3.0 |
| subagent | mutation | openai.gpt-6.1-terra | 131 | 0 | 540278 | 11252 | 457720 | 82296 | yes | 1032.143500 | 1920.101292 | no | 0.000262 | 0.056260 | 0.022886 | 0.102870 | 0.056522 | 0.125756 | 0.182278 | 1.3.0 |
| subagent | research | openai.gpt-6.1-terra | 52 | 0 | 1035363 | 15755 | 613564 | 421695 | yes | 1506.506541 | 4705.513250 | no | 0.000104 | 0.078775 | 0.030678 | 0.527119 | 0.078879 | 0.557797 | 0.636676 | 1.3.0 |
| supervisor | supervisor | openai.gpt-6.1-sol | 268 | 0 | 1579162 | 39285 | 1086832 | 491794 | yes | 1829.730625 | 5089.577459 | no | 0.001072 | 0.392850 | 0.108683 | 1.229485 | 0.393922 | 1.338168 | 1.732090 | 1.3.0 |
| tool | analytics | openai.gpt-6-luna | 10 | 0 | 3280 | 500 | 0 | 0 | yes | 609.184542 | 925.784292 | no | 0.000328 | 0.000250 | 0.000000 | 0.000000 | 0.000578 | 0.000000 | 0.000578 | 1.3.0 |
| tool | mutation | openai.gpt-6-luna | 78 | 0 | 25823 | 7697 | 0 | 0 | yes | 794.333375 | 1346.836916 | no | 0.002582 | 0.003848 | 0.000000 | 0.000000 | 0.006431 | 0.000000 | 0.006431 | 1.3.0 |
| tool | research | openai.gpt-6-luna | 106 | 0 | 23202 | 8053 | 0 | 0 | yes | 593.677375 | 2147.219084 | no | 0.002320 | 0.004027 | 0.000000 | 0.000000 | 0.006347 | 0.000000 | 0.006347 | 1.3.0 |

Per-tier summary:

| tier | input_tokens | output_tokens | cost | latency_p50_ms | latency_p95_ms | success (met/applicable) | success_rate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| supervisor | 1579162 | 39285 | 1.732090 | 1829.730625 | 5089.577459 | 119/120 | 0.991667 |
| subagent | 1597649 | 28195 | 0.845938 | 1163.972458 | 3181.016541 | 114/115 | 0.991304 |
| tool | 52305 | 16250 | 0.013356 | 620.995708 | 1462.112541 | 94/95 | 0.989474 |

Per tool:

| tool_name | role | count | call_latency_ms_p50 | call_latency_ms_p95 | truncated_count | outcome_data | outcome_no_match | outcome_denied | outcome_tool_failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| create_note | mutation | 10 | 302.428000 | 494.479000 | 0 | 10 | 0 | 0 | 0 |
| get_account | mutation | 10 | 265.506333 | 543.125542 | 0 | 10 | 0 | 0 | 0 |
| get_account | research | 10 | 266.027083 | 400.150750 | 0 | 10 | 0 | 0 | 0 |
| get_account | supervisor | 9 | 285.640417 | 2040.909209 | 0 | 9 | 0 | 0 | 0 |
| get_contact | mutation | 3 | 241.969708 | 345.087833 | 0 | 3 | 0 | 0 | 0 |
| get_contact | research | 23 | 245.550875 | 279.122250 | 0 | 23 | 0 | 0 | 0 |
| get_contact | supervisor | 20 | 240.077833 | 278.984000 | 0 | 20 | 0 | 0 | 0 |
| get_opportunity | mutation | 6 | 297.985000 | 399.172500 | 1 | 6 | 0 | 0 | 0 |
| get_opportunity | research | 45 | 303.546875 | 400.061709 | 10 | 45 | 0 | 0 | 0 |
| get_opportunity | supervisor | 31 | 302.345458 | 423.821875 | 10 | 21 | 5 | 5 | 0 |
| list_activities | research | 23 | 321.798875 | 425.401500 | 10 | 22 | 0 | 0 | 1 |
| list_activities | supervisor | 15 | 291.893333 | 482.857333 | 6 | 10 | 0 | 0 | 5 |
| list_proposals | supervisor | 6 | 320.169625 | 491.883416 | 0 | 6 | 0 | 0 | 0 |
| log_activity | mutation | 10 | 324.022500 | 481.500125 | 0 | 10 | 0 | 0 | 0 |
| pipeline_aggregate | supervisor | 5 | 472.878166 | 488.353833 | 0 | 5 | 0 | 0 | 0 |
| search_accounts | mutation | 5 | 288.172875 | 415.660791 | 0 | 5 | 0 | 0 | 0 |
| search_opportunities | analytics | 10 | 158.110166 | 297.919458 | 0 | 5 | 0 | 5 | 0 |
| search_opportunities | research | 5 | 273.246833 | 294.147833 | 0 | 5 | 0 | 0 | 0 |
| search_opportunities | supervisor | 5 | 103.697292 | 121.684792 | 0 | 0 | 0 | 5 | 0 |
| submit_proposal | mutation | 24 | 322.448083 | 428.097167 | 0 | 24 | 0 | 0 | 0 |
| update_account | mutation | 5 | 326.202875 | 465.208625 | 0 | 5 | 0 | 0 | 0 |
| update_contact | mutation | 5 | 319.517583 | 495.231125 | 0 | 5 | 0 | 0 | 0 |

Excluded invocations: 0 total (cold_start: 0, retry: 0, throttle: 0)

### Aggregate Score

| field | value |
| --- | --- |
| scored_task_count | 120 |
| unscored_task_count | 0 |
| mean_correctness | 0.999074 |
| bad_write_rate | 0.000000 |
| tool_call_correct_rate | 1.000000 |

`bad_write_rate` basis: all 255 declared `must_not_change` assertion(s) in this arm had a captured pre-turn baseline, so the figure is a measurement over them (src/eval/matrix.py).

`mean_correctness` composition: negative guards a do-nothing turn satisfies are 0.8141 of the assertion mass this figure averages — the mean over the 120 scored tasks of each task's own guard share, weighted exactly as `mean_correctness` itself is, rather than the ratio pooled over assertions (pooled totals, for context and not the basis of the figure just quoted: 679 guard assertions and 150 positive ones across those tasks). So the figure is not a task-achievement rate. Taken alone, 149 of 150 positive assertions were satisfied (0.9933); the guards alone were satisfied at 1.0000 (src/eval/matrix.py).

Retrieval-narrowed task deduction: the assertions this arm did not satisfy on the tasks narrowed to retrieval account for at most 0.000000 of its `mean_correctness` (`gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000), an upper bound over the 120 scored tasks derived from this arm's own per-task records. The figure itself is unchanged by this disclosure (src/eval/golden_tasks.yaml).

### Per-Task Results

| task_id | scoring_mode | correctness | unscored_reason | bad_write | tool_call_correct | truncated | budget_exhausted |
| --- | --- | --- | --- | --- | --- | --- | --- |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | yes | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | yes | no |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | yes | no |
| gt-06-enrichment-create-note | machine | 1.000000 | — | no | yes | no | no |
| gt-07-enrichment-log-activity-last-touch | machine | 1.000000 | — | no | yes | no | no |
| gt-08-enrichment-stage-advance-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-09-enrichment-value-and-close-date-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-10-enrichment-update-account-fields | machine | 1.000000 | — | no | yes | no | no |
| gt-11-denial-out-of-book-target | machine | 1.000000 | — | no | yes | no | no |
| gt-12-denial-nonexistent-target | machine | 1.000000 | — | no | yes | no | no |
| gt-13-denial-cross-account-comparison | machine | 1.000000 | — | no | yes | no | no |
| gt-14-denial-departed-contact-update | machine | 1.000000 | — | no | yes | no | no |
| gt-15-denial-service-principal-no-implicit-visibility | machine | 1.000000 | — | no | yes | no | no |
| gt-16-denial-tool-failure-not-conflated | machine | 1.000000 | — | no | yes | no | no |
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | yes | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | yes | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | yes | no |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | yes | no |
| gt-06-enrichment-create-note | machine | 1.000000 | — | no | yes | no | no |
| gt-07-enrichment-log-activity-last-touch | machine | 1.000000 | — | no | yes | no | no |
| gt-08-enrichment-stage-advance-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-09-enrichment-value-and-close-date-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-10-enrichment-update-account-fields | machine | 1.000000 | — | no | yes | no | no |
| gt-11-denial-out-of-book-target | machine | 1.000000 | — | no | yes | no | no |
| gt-12-denial-nonexistent-target | machine | 1.000000 | — | no | yes | no | no |
| gt-13-denial-cross-account-comparison | machine | 1.000000 | — | no | yes | no | no |
| gt-14-denial-departed-contact-update | machine | 1.000000 | — | no | yes | no | no |
| gt-15-denial-service-principal-no-implicit-visibility | machine | 1.000000 | — | no | yes | no | no |
| gt-16-denial-tool-failure-not-conflated | machine | 1.000000 | — | no | yes | no | no |
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | yes | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | yes | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | yes | no |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | yes | no |
| gt-06-enrichment-create-note | machine | 1.000000 | — | no | yes | no | no |
| gt-07-enrichment-log-activity-last-touch | machine | 1.000000 | — | no | yes | no | no |
| gt-08-enrichment-stage-advance-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-09-enrichment-value-and-close-date-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-10-enrichment-update-account-fields | machine | 1.000000 | — | no | yes | no | no |
| gt-11-denial-out-of-book-target | machine | 1.000000 | — | no | yes | no | no |
| gt-12-denial-nonexistent-target | machine | 1.000000 | — | no | yes | no | no |
| gt-13-denial-cross-account-comparison | machine | 1.000000 | — | no | yes | no | no |
| gt-14-denial-departed-contact-update | machine | 1.000000 | — | no | yes | no | no |
| gt-15-denial-service-principal-no-implicit-visibility | machine | 1.000000 | — | no | yes | no | no |
| gt-16-denial-tool-failure-not-conflated | machine | 1.000000 | — | no | yes | no | no |
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | yes | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 0.888889 | — | no | yes | yes | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | yes | no |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | yes | no |
| gt-06-enrichment-create-note | machine | 1.000000 | — | no | yes | no | no |
| gt-07-enrichment-log-activity-last-touch | machine | 1.000000 | — | no | yes | no | no |
| gt-08-enrichment-stage-advance-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-09-enrichment-value-and-close-date-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-10-enrichment-update-account-fields | machine | 1.000000 | — | no | yes | no | no |
| gt-11-denial-out-of-book-target | machine | 1.000000 | — | no | yes | no | no |
| gt-12-denial-nonexistent-target | machine | 1.000000 | — | no | yes | no | no |
| gt-13-denial-cross-account-comparison | machine | 1.000000 | — | no | yes | no | no |
| gt-14-denial-departed-contact-update | machine | 1.000000 | — | no | yes | no | no |
| gt-15-denial-service-principal-no-implicit-visibility | machine | 1.000000 | — | no | yes | no | no |
| gt-16-denial-tool-failure-not-conflated | machine | 1.000000 | — | no | yes | no | no |
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | yes | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | yes | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | yes | no |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | yes | no |
| gt-06-enrichment-create-note | machine | 1.000000 | — | no | yes | no | no |
| gt-07-enrichment-log-activity-last-touch | machine | 1.000000 | — | no | yes | no | no |
| gt-08-enrichment-stage-advance-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-09-enrichment-value-and-close-date-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-10-enrichment-update-account-fields | machine | 1.000000 | — | no | yes | no | no |
| gt-11-denial-out-of-book-target | machine | 1.000000 | — | no | yes | no | no |
| gt-12-denial-nonexistent-target | machine | 1.000000 | — | no | yes | no | no |
| gt-13-denial-cross-account-comparison | machine | 1.000000 | — | no | yes | no | no |
| gt-14-denial-departed-contact-update | machine | 1.000000 | — | no | yes | no | no |
| gt-15-denial-service-principal-no-implicit-visibility | machine | 1.000000 | — | no | yes | no | no |
| gt-16-denial-tool-failure-not-conflated | machine | 1.000000 | — | no | yes | no | no |
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | yes | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |

Per-task correctness spread across repeats:

| task_id | repeats | correctness_min | correctness_mean | correctness_max | success_fraction |
| --- | --- | --- | --- | --- | --- |
| gt-01-investigation-pd01-successor | 5 | 0.888889 | 0.977778 | 1.000000 | 0.800000 |
| gt-02-investigation-pd02-stale-forecast | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-03-investigation-pd03-scoped-lookup | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-04-investigation-turn-budget-partial | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-05-investigation-large-tool-result | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-06-enrichment-create-note | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-07-enrichment-log-activity-last-touch | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-08-enrichment-stage-advance-proposal | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-09-enrichment-value-and-close-date-proposal | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-10-enrichment-update-account-fields | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-11-denial-out-of-book-target | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-12-denial-nonexistent-target | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-13-denial-cross-account-comparison | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-14-denial-departed-contact-update | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-15-denial-service-principal-no-implicit-visibility | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-16-denial-tool-failure-not-conflated | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-17-lookup-opportunity-detail | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-18-lookup-account-contacts | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-19-lookup-pipeline-aggregate | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-20-lookup-proposal-queue | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-21-enrichment-pd04-multi-field-proposal | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-22-investigation-pd05-report-only | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-23-investigation-pd06-ambiguous-contact | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-24-enrichment-pd07-multi-step-write | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
