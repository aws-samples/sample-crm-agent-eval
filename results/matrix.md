# Model Evaluation Matrix

## Tier comparison summary

Per-tier figures for each arm, one column per arm in run order; the notes below the tables define each row.

### Supervisor

| Metric | anthropic | openai |
| --- | --- | --- |
| Model | anthropic.claude-sonnet-5-5 | openai.gpt-6.2-sol |
| Effort | medium | medium |
| Task completion rate | 0.950000 (228/240) | 0.991667 (238/240) |
| Task completion over repeats | 0.958333 (23/24, majority of 10) | 1.000000 (24/24, majority of 10) |
| Task accuracy | 0.985833 | 0.999074 |
| Cost per completed task | 0.020882 | 0.014848 |
| P50 latency per completed task (ms) | 3628.703291 (n=228) | 2061.440209 (n=238) |
| P95 latency per completed task (ms) | 15244.208750 (n=228) | 11550.794584 (n=238) |
| Model invocations | 569 | 581 |
| Long-context invocations | 0 | 0 |
| Total input tokens | 4466182 | 3762143 |
| Cache read tokens | 3229788 | 2805322 |
| Cache write tokens | 1234776 | 956821 |
| Uncached input tokens | 1618 | 0 |
| Total output tokens | 134791 | 86128 |
| Cost per applicable task | 0.019838 | 0.014724 |
| Self-corrected retries | 0 | 2 |
| Budget-exhausted tasks | 0 | 0 |
| Truncated tool calls | 38 | 36 |
| Truncated tasks | 38 | 35 |
| Output-truncated model calls | 0 | 0 |
| Output-truncated tasks | 0 | 0 |

### Subagent

| Metric | anthropic | openai |
| --- | --- | --- |
| Model | anthropic.claude-sonnet-5-5 | openai.gpt-6.1-terra |
| Effort | low | low |
| Task completion rate | 1.000000 (230/230) | 0.995652 (229/230) |
| Task completion over repeats | 1.000000 (23/23, majority of 10) | 1.000000 (23/23, majority of 10) |
| Task accuracy | 1.000000 | 0.995652 |
| Cost per completed task | 0.006041 | 0.003726 |
| P50 latency per completed task (ms) | 4002.407125 (n=107) | 2349.741916 (n=116) |
| P95 latency per completed task (ms) | 10235.889251 (n=107) | 12898.266416 (n=116) |
| Model invocations | 237 | 323 |
| Long-context invocations | 0 | 0 |
| Total input tokens | 1878883 | 1897809 |
| Cache read tokens | 1614316 | 1414389 |
| Cache write tokens | 263879 | 482774 |
| Uncached input tokens | 688 | 646 |
| Total output tokens | 56701 | 35700 |
| Cost per applicable task | 0.006041 | 0.003710 |
| Self-corrected retries | 8 | 4 |
| Budget-exhausted tasks | 0 | 0 |
| Truncated tool calls | not applicable | not applicable |
| Truncated tasks | not applicable | not applicable |
| Output-truncated model calls | 0 | 0 |
| Output-truncated tasks | 0 | 0 |

### ToolCall

| Metric | anthropic | openai |
| --- | --- | --- |
| Model | anthropic.claude-haiku-5-5 | openai.gpt-6-luna |
| Effort | low | low |
| Task completion rate | 1.000000 (190/190) | 0.989474 (188/190) |
| Task completion over repeats | 1.000000 (19/19, majority of 10) | 1.000000 (19/19, majority of 10) |
| Task accuracy | 1.000000 | 0.989474 |
| Cost per completed task | 0.000184 | 0.000110 |
| P50 latency per completed task (ms) | 1967.623959 (n=107) | 932.496166 (n=116) |
| P95 latency per completed task (ms) | 8722.435292 (n=107) | 6922.391334 (n=116) |
| Model invocations | 166 | 245 |
| Long-context invocations | 0 | 0 |
| Total input tokens | 142694 | 76080 |
| Cache read tokens | 0 | 0 |
| Cache write tokens | 122567 | 0 |
| Uncached input tokens | 20127 | 76080 |
| Total output tokens | 35248 | 26098 |
| Cost per applicable task | 0.000184 | 0.000109 |
| Self-corrected retries | not applicable | not applicable |
| Budget-exhausted tasks | 0 | 0 |
| Truncated tool calls | 3 | 20 |
| Truncated tasks | 3 | 14 |
| Output-truncated model calls | 0 | 0 |
| Output-truncated tasks | 0 | 0 |

Notes:

- Model lists the tier's configured model and every model that spent tokens at the tier; Effort lists the efforts the tier's turns ran at, with unsupported for a model that takes no effort setting. Task completion rate is rate (met/applicable) over the arm's tasks applicable at the tier, where n is the applicable task count: at Supervisor a task's correctness is 1.0, at Subagent the observed delegation outcome equals the expected one, and at ToolCall an expected tool was called with schema-valid arguments. Task accuracy is the arm's mean_correctness at Supervisor and the completion rate at Subagent and ToolCall. Task completion over repeats is rate (completed/applicable) over the arm's tasks applicable at the tier in any repeat, where a task counts when its tier expectation was met in a strict majority of its applicable repeats, and N is the per-task repeat count. Cost per completed task divides the tier's total cost by its completed task count, so spend on tasks that did not complete is included; Cost per applicable task divides the same total by the tier's applicable task count (n of Task completion rate), so the two rows are equal when every applicable task completed, and it reads — when no task was applicable. Latency per completed task sums the tier's measured call latencies within each completed task and takes the nearest-rank percentile over those tasks; a completed task with no measured call at the tier contributes no sample, and n counts the contributing tasks. Model invocations sums the tier's included invocations; Long-context invocations counts those priced at the model's long-context rates, and reads — for a run recorded before that count existed. Self-corrected retries counts the tier's tool calls whose tool_failure result was followed, later in the same task, by a call to the same tool from the same role that returned data or no_match; such a call does not set the task's delegation outcome. ToolCall makes one call per invocation, so that row is not applicable there. Budget-exhausted tasks counts the task runs whose turn budget ran out at the tier. Truncated tool calls counts the tier's tool calls whose result was truncated, the Supervisor's own direct calls at Supervisor and every call made inside a delegation at ToolCall; Truncated tasks counts the task runs with at least one such call. Subagent makes no tool call itself, so both tool-result truncation rows are not applicable there. Output-truncated model calls counts the tier's model calls that stopped at the shared per-call output token cap before finishing; Output-truncated tasks counts the task runs with at least one such call at the tier. A task whose calls were cut off at two tiers counts once in each tier's row, and a task that was both output-truncated and budget-exhausted counts in both rows. These rows apply at every tier and are distinct from the tool-result truncation rows above.
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
- Retrieval-narrowing magnitude: the largest deduction any one arm's retrieval-narrowed tasks account for is 0.000000 of that arm's `mean_correctness`, which is smaller than the largest absolute difference between any two arms' `mean_correctness` in this sweep (0.013241). Which arm sits on which side of that difference is deliberately not computed or stated anywhere in this artifact; the comparison is published because a disclosed measurement conservatism of this size bounds how much the difference between arms can be read as a property of the models (src/eval/golden_tasks.yaml).
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
| timestamp | 2026-11-21T23:13:27.899350+00:00 |
| model_id | anthropic.claude-sonnet-5-5 |
| effort | medium |
| dataset_seed | 20260101 |
| runtime_identifier | live-aws-lambda-dispatch |
| price_table_version | 1.4.0 |
| answer_style_sha256 | 953efa6d0d26da1fb09cf2cdf625c78f0bd8f7d04da7652e581e623edea67f47 |
| prompt_bundle_sha256 | e01e857a8511646bddf80e96d525e64983df062c944aea5d291767be4f504c9b |
| prompt_caching | explicit: cache_control {type: ephemeral} (5-minute TTL) on the last stable system block and on the last content block of the latest message, at most 4 points, each omitted when its cumulative prefix is below prompt_cache_min_checkpoint_tokens; beside the platform's implicit caching, which no request field turns off |
| output_token_cap | 16384 |
| dirty_tree | no |

### Usage and Cost

Supervisor tier only: these figures cover the arm's supervisor-tier calls to anthropic.claude-sonnet-5-5.

| field | value |
| --- | --- |
| model_id | anthropic.claude-sonnet-5-5 |
| included_invocation_count | 569 |
| long_context_invocation_count | 0 |
| input_tokens | 1618 |
| output_tokens | 134791 |
| cache_write_tokens | 1234776 |
| cache_read_tokens | 3229788 |
| cache_min_checkpoint_tokens | 512 |
| latency_measured | yes |
| latency_percentile_method | nearest-rank |
| task_latency_ms_p50 | 7636.318084 |
| task_latency_ms_p95 | 22279.113750 |
| cost_withheld_provisional | no |
| cost_input | 0.003236 |
| cost_output | 1.347910 |
| cost_cache_write | 3.086940 |
| cost_cache_read | 0.322979 |
| cost_excluding_cache | 1.351146 |
| cost_cache_total | 3.409919 |
| cost_total | 4.761065 |
| price_table_version | 1.4.0 |

All tiers (every call the arm made):

| field | value |
| --- | --- |
| arm_cost_total | 6.185537 |
| arm_long_context_invocation_count | 0 |

Call latency by tier (ms):

| tier | p50 | p95 | count |
| --- | --- | --- | --- |
| supervisor | 1615.180084 | 4470.792083 | 569 |
| subagent | 1829.206208 | 3841.483708 | 237 |
| tool | 1831.509833 | 2532.105333 | 166 |

Per tier / role / model:

| tier | role | model_id | included_invocation_count | long_context_invocation_count | input_tokens | output_tokens | cache_read_tokens | cache_write_tokens | call_latency_measured | call_latency_ms_p50 | call_latency_ms_p95 | cost_withheld_provisional | cost_input | cost_output | cost_cache_read | cost_cache_write | cost_excluding_cache | cost_cache_total | cost_total | price_table_version |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| subagent | mutation | anthropic.claude-sonnet-5-5 | 233 | 0 | 676 | 52741 | 1609348 | 193648 | yes | 1828.896833 | 3477.674541 | no | 0.001352 | 0.527410 | 0.160935 | 0.484120 | 0.528762 | 0.645055 | 1.173817 | 1.4.0 |
| subagent | research | anthropic.claude-sonnet-5-5 | 4 | 0 | 12 | 3960 | 4968 | 70231 | yes | 4548.123083 | 7248.018584 | no | 0.000024 | 0.039600 | 0.000497 | 0.175577 | 0.039624 | 0.176074 | 0.215698 | 1.4.0 |
| supervisor | supervisor | anthropic.claude-sonnet-5-5 | 569 | 0 | 1618 | 134791 | 3229788 | 1234776 | yes | 1615.180084 | 4470.792083 | no | 0.003236 | 1.347910 | 0.322979 | 3.086940 | 1.351146 | 3.409919 | 4.761065 | 1.4.0 |
| tool | mutation | anthropic.claude-haiku-5-5 | 144 | 0 | 8019 | 33336 | 0 | 122567 | yes | 1888.813375 | 2532.105333 | no | 0.000802 | 0.016668 | 0.000000 | 0.015321 | 0.017470 | 0.015321 | 0.032791 | 1.4.0 |
| tool | research | anthropic.claude-haiku-5-5 | 22 | 0 | 12108 | 1912 | 0 | 0 | yes | 1373.158250 | 1634.174917 | no | 0.001211 | 0.000956 | 0.000000 | 0.000000 | 0.002167 | 0.000000 | 0.002167 | 1.4.0 |

Per-tier summary:

| tier | input_tokens | output_tokens | cost | latency_p50_ms | latency_p95_ms | success (met/applicable) | success_rate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| supervisor | 1618 | 134791 | 4.761065 | 1615.180084 | 4470.792083 | 228/240 | 0.950000 |
| subagent | 688 | 56701 | 1.389515 | 1829.206208 | 3841.483708 | 230/230 | 1.000000 |
| tool | 20127 | 35248 | 0.034958 | 1831.509833 | 2532.105333 | 190/190 | 1.000000 |

Per tool:

| tool_name | role | count | call_latency_ms_p50 | call_latency_ms_p95 | truncated_count | outcome_data | outcome_no_match | outcome_denied | outcome_tool_failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| create_note | mutation | 21 | 298.892000 | 430.468709 | 0 | 20 | 0 | 0 | 1 |
| get_account | mutation | 5 | 290.601875 | 325.549500 | 0 | 5 | 0 | 0 | 0 |
| get_account | supervisor | 38 | 271.731875 | 396.737375 | 0 | 38 | 0 | 0 | 0 |
| get_contact | mutation | 2 | 249.058458 | 277.815792 | 0 | 2 | 0 | 0 | 0 |
| get_contact | research | 6 | 240.870708 | 310.517750 | 0 | 6 | 0 | 0 | 0 |
| get_contact | supervisor | 36 | 252.995625 | 378.493833 | 0 | 36 | 0 | 0 | 0 |
| get_opportunity | mutation | 1 | 360.222333 | 360.222333 | 1 | 1 | 0 | 0 | 0 |
| get_opportunity | research | 16 | 307.104958 | 365.030708 | 2 | 16 | 0 | 0 | 0 |
| get_opportunity | supervisor | 136 | 306.298875 | 401.701042 | 28 | 116 | 10 | 10 | 0 |
| list_activities | supervisor | 38 | 313.839250 | 475.705333 | 10 | 28 | 0 | 0 | 10 |
| list_proposals | mutation | 6 | 279.995166 | 454.720041 | 0 | 6 | 0 | 0 | 0 |
| list_proposals | supervisor | 11 | 293.167667 | 1975.027667 | 0 | 11 | 0 | 0 | 0 |
| log_activity | mutation | 20 | 339.981125 | 446.171458 | 0 | 20 | 0 | 0 | 0 |
| pipeline_aggregate | supervisor | 10 | 476.591791 | 2253.853292 | 0 | 10 | 0 | 0 | 0 |
| search_accounts | supervisor | 10 | 244.914667 | 2006.244042 | 0 | 10 | 0 | 0 | 0 |
| search_opportunities | supervisor | 40 | 141.708167 | 280.282042 | 0 | 20 | 0 | 20 | 0 |
| submit_proposal | mutation | 69 | 300.450875 | 401.379125 | 0 | 62 | 0 | 0 | 7 |
| update_account | mutation | 10 | 300.569833 | 1951.392791 | 0 | 10 | 0 | 0 | 0 |
| update_contact | mutation | 10 | 294.204750 | 2115.391916 | 0 | 10 | 0 | 0 | 0 |

Excluded invocations: 0 total (cold_start: 0, retry: 0, throttle: 0)

### Aggregate Score

| field | value |
| --- | --- |
| scored_task_count | 240 |
| unscored_task_count | 0 |
| mean_correctness | 0.985833 |
| bad_write_rate | 0.000000 |
| tool_call_correct_rate | 1.000000 |

`bad_write_rate` basis: all 510 declared `must_not_change` assertion(s) in this arm had a captured pre-turn baseline, so the figure is a measurement over them (src/eval/matrix.py).

`mean_correctness` composition: negative guards a do-nothing turn satisfies are 0.8147 of the assertion mass this figure averages — the mean over the 240 scored tasks of each task's own guard share, weighted exactly as `mean_correctness` itself is, rather than the ratio pooled over assertions (pooled totals, for context and not the basis of the figure just quoted: 1372 guard assertions and 300 positive ones across those tasks). So the figure is not a task-achievement rate. Taken alone, 290 of 300 positive assertions were satisfied (0.9667); the guards alone were satisfied at 0.9825 (src/eval/matrix.py).

Retrieval-narrowed task deduction: the assertions this arm did not satisfy on the tasks narrowed to retrieval account for at most 0.000000 of its `mean_correctness` (`gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000), an upper bound over the 240 scored tasks derived from this arm's own per-task records. The figure itself is unchanged by this disclosure (src/eval/golden_tasks.yaml).

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
| gt-09-enrichment-value-and-close-date-proposal | machine | 0.800000 | — | no | yes | yes | no |
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
| gt-01-investigation-pd01-successor | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-02-investigation-pd02-stale-forecast | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-03-investigation-pd03-scoped-lookup | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-04-investigation-turn-budget-partial | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-05-investigation-large-tool-result | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-06-enrichment-create-note | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-07-enrichment-log-activity-last-touch | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-08-enrichment-stage-advance-proposal | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-09-enrichment-value-and-close-date-proposal | 10 | 0.800000 | 0.960000 | 1.000000 | 0.800000 |
| gt-10-enrichment-update-account-fields | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-11-denial-out-of-book-target | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-12-denial-nonexistent-target | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-13-denial-cross-account-comparison | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-14-denial-departed-contact-update | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-15-denial-service-principal-no-implicit-visibility | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-16-denial-tool-failure-not-conflated | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-17-lookup-opportunity-detail | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-18-lookup-account-contacts | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-19-lookup-pipeline-aggregate | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-20-lookup-proposal-queue | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-21-enrichment-pd04-multi-field-proposal | 10 | 0.700000 | 0.700000 | 0.700000 | 0.000000 |
| gt-22-investigation-pd05-report-only | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-23-investigation-pd06-ambiguous-contact | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-24-enrichment-pd07-multi-step-write | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |

## openai.gpt-6.2-sol (live-mode run)

### Provenance

| field | value |
| --- | --- |
| mode | live |
| timestamp | 2026-11-21T23:44:04.625483+00:00 |
| model_id | openai.gpt-6.2-sol |
| effort | medium |
| dataset_seed | 20260101 |
| runtime_identifier | live-aws-lambda-dispatch |
| price_table_version | 1.4.0 |
| answer_style_sha256 | 953efa6d0d26da1fb09cf2cdf625c78f0bd8f7d04da7652e581e623edea67f47 |
| prompt_bundle_sha256 | e01e857a8511646bddf80e96d525e64983df062c944aea5d291767be4f504c9b |
| prompt_caching | explicit: prompt_cache_breakpoint {mode: explicit} on the last stable system input_text block, beside implicit caching (prompt_cache_options {mode: implicit, ttl: 30m}, an automatic breakpoint on the latest message); minimum prefix from prompt_cache_min_checkpoint_tokens |
| output_token_cap | 16384 |
| dirty_tree | no |

### Usage and Cost

Supervisor tier only: these figures cover the arm's supervisor-tier calls to openai.gpt-6.2-sol.

| field | value |
| --- | --- |
| model_id | openai.gpt-6.2-sol |
| included_invocation_count | 581 |
| long_context_invocation_count | 0 |
| input_tokens | 3762143 |
| output_tokens | 86128 |
| cache_write_tokens | 956821 |
| cache_read_tokens | 2805322 |
| cache_min_checkpoint_tokens | 1024 |
| latency_measured | yes |
| latency_percentile_method | nearest-rank |
| task_latency_ms_p50 | 4909.170500 |
| task_latency_ms_p95 | 24431.462792 |
| cost_withheld_provisional | no |
| cost_input | 0.000000 |
| cost_output | 0.861280 |
| cost_cache_write | 2.392053 |
| cost_cache_read | 0.280532 |
| cost_excluding_cache | 0.861280 |
| cost_cache_total | 2.672585 |
| cost_total | 3.533865 |
| price_table_version | 1.4.0 |

All tiers (every call the arm made):

| field | value |
| --- | --- |
| arm_cost_total | 4.407855 |
| arm_long_context_invocation_count | 0 |

Call latency by tier (ms):

| tier | p50 | p95 | count |
| --- | --- | --- | --- |
| supervisor | 1027.165959 | 2824.909625 | 581 |
| subagent | 1054.738167 | 3030.960167 | 323 |
| tool | 719.811250 | 2147.963291 | 245 |

Per tier / role / model:

| tier | role | model_id | included_invocation_count | long_context_invocation_count | input_tokens | output_tokens | cache_read_tokens | cache_write_tokens | call_latency_measured | call_latency_ms_p50 | call_latency_ms_p95 | cost_withheld_provisional | cost_input | cost_output | cost_cache_read | cost_cache_write | cost_excluding_cache | cost_cache_total | cost_total | price_table_version |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| subagent | mutation | openai.gpt-6.1-terra | 243 | 0 | 934956 | 18588 | 830703 | 103767 | yes | 1022.061709 | 1978.068791 | no | 0.000486 | 0.092940 | 0.041535 | 0.129709 | 0.093426 | 0.171244 | 0.264670 | 1.4.0 |
| subagent | research | openai.gpt-6.1-terra | 80 | 0 | 962853 | 17112 | 583686 | 379007 | yes | 1198.212709 | 5938.494000 | no | 0.000160 | 0.085560 | 0.029184 | 0.473759 | 0.085720 | 0.502943 | 0.588663 | 1.4.0 |
| supervisor | supervisor | openai.gpt-6.2-sol | 581 | 0 | 3762143 | 86128 | 2805322 | 956821 | yes | 1027.165959 | 2824.909625 | no | 0.000000 | 0.861280 | 0.280532 | 2.392052 | 0.861280 | 2.672585 | 3.533865 | 1.4.0 |
| tool | mutation | openai.gpt-6-luna | 134 | 0 | 46427 | 13294 | 0 | 0 | yes | 790.664125 | 1114.770834 | no | 0.004643 | 0.006647 | 0.000000 | 0.000000 | 0.011290 | 0.000000 | 0.011290 | 1.4.0 |
| tool | research | openai.gpt-6-luna | 111 | 0 | 29653 | 12804 | 0 | 0 | yes | 606.052417 | 2249.271625 | no | 0.002965 | 0.006402 | 0.000000 | 0.000000 | 0.009367 | 0.000000 | 0.009367 | 1.4.0 |

Per-tier summary:

| tier | input_tokens | output_tokens | cost | latency_p50_ms | latency_p95_ms | success (met/applicable) | success_rate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| supervisor | 3762143 | 86128 | 3.533865 | 1027.165959 | 2824.909625 | 238/240 | 0.991667 |
| subagent | 1897809 | 35700 | 0.853333 | 1054.738167 | 3030.960167 | 229/230 | 0.995652 |
| tool | 76080 | 26098 | 0.020657 | 719.811250 | 2147.963291 | 188/190 | 0.989474 |

Per tool:

| tool_name | role | count | call_latency_ms_p50 | call_latency_ms_p95 | truncated_count | outcome_data | outcome_no_match | outcome_denied | outcome_tool_failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| create_note | mutation | 20 | 303.194209 | 392.473625 | 0 | 20 | 0 | 0 | 0 |
| get_account | mutation | 19 | 272.145667 | 367.569792 | 0 | 19 | 0 | 0 | 0 |
| get_account | research | 7 | 255.985875 | 271.382000 | 0 | 7 | 0 | 0 | 0 |
| get_account | supervisor | 26 | 268.955958 | 305.198750 | 0 | 26 | 0 | 0 | 0 |
| get_contact | mutation | 1 | 224.583542 | 224.583542 | 0 | 1 | 0 | 0 | 0 |
| get_contact | research | 12 | 240.802542 | 274.522084 | 0 | 12 | 0 | 0 | 0 |
| get_contact | supervisor | 34 | 241.076375 | 280.426833 | 0 | 34 | 0 | 0 | 0 |
| get_opportunity | mutation | 2 | 279.862666 | 290.705167 | 0 | 2 | 0 | 0 | 0 |
| get_opportunity | research | 59 | 301.373125 | 412.356834 | 10 | 59 | 0 | 0 | 0 |
| get_opportunity | supervisor | 95 | 310.478042 | 409.164458 | 24 | 75 | 10 | 10 | 0 |
| list_activities | research | 33 | 366.242750 | 462.790209 | 10 | 28 | 0 | 0 | 5 |
| list_activities | supervisor | 56 | 292.345667 | 473.071416 | 12 | 44 | 0 | 0 | 12 |
| list_audit_records | supervisor | 1 | 1833.335291 | 1833.335291 | 0 | 1 | 0 | 0 | 0 |
| list_proposals | supervisor | 10 | 267.922459 | 1951.042709 | 0 | 10 | 0 | 0 | 0 |
| log_activity | mutation | 20 | 321.803708 | 361.064875 | 0 | 20 | 0 | 0 | 0 |
| pipeline_aggregate | supervisor | 10 | 449.550917 | 2036.151333 | 0 | 10 | 0 | 0 | 0 |
| search_accounts | mutation | 4 | 240.832542 | 322.646000 | 0 | 4 | 0 | 0 | 0 |
| search_accounts | supervisor | 6 | 257.923375 | 356.053500 | 0 | 6 | 0 | 0 | 0 |
| search_opportunities | supervisor | 40 | 138.002250 | 319.154125 | 0 | 20 | 0 | 20 | 0 |
| submit_proposal | mutation | 48 | 301.106083 | 433.763375 | 0 | 48 | 0 | 0 | 0 |
| update_account | mutation | 10 | 326.084333 | 396.816417 | 0 | 10 | 0 | 0 | 0 |
| update_contact | mutation | 10 | 291.071833 | 391.281166 | 0 | 10 | 0 | 0 | 0 |

Excluded invocations: 0 total (cold_start: 0, retry: 0, throttle: 0)

### Aggregate Score

| field | value |
| --- | --- |
| scored_task_count | 240 |
| unscored_task_count | 0 |
| mean_correctness | 0.999074 |
| bad_write_rate | 0.000000 |
| tool_call_correct_rate | 1.000000 |

`bad_write_rate` basis: all 510 declared `must_not_change` assertion(s) in this arm had a captured pre-turn baseline, so the figure is a measurement over them (src/eval/matrix.py).

`mean_correctness` composition: negative guards a do-nothing turn satisfies are 0.8141 of the assertion mass this figure averages — the mean over the 240 scored tasks of each task's own guard share, weighted exactly as `mean_correctness` itself is, rather than the ratio pooled over assertions (pooled totals, for context and not the basis of the figure just quoted: 1358 guard assertions and 300 positive ones across those tasks). So the figure is not a task-achievement rate. Taken alone, 298 of 300 positive assertions were satisfied (0.9933); the guards alone were satisfied at 1.0000 (src/eval/matrix.py).

Retrieval-narrowed task deduction: the assertions this arm did not satisfy on the tasks narrowed to retrieval account for at most 0.000000 of its `mean_correctness` (`gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000), an upper bound over the 240 scored tasks derived from this arm's own per-task records. The figure itself is unchanged by this disclosure (src/eval/golden_tasks.yaml).

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

Per-task correctness spread across repeats:

| task_id | repeats | correctness_min | correctness_mean | correctness_max | success_fraction |
| --- | --- | --- | --- | --- | --- |
| gt-01-investigation-pd01-successor | 10 | 0.888889 | 0.977778 | 1.000000 | 0.800000 |
| gt-02-investigation-pd02-stale-forecast | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-03-investigation-pd03-scoped-lookup | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-04-investigation-turn-budget-partial | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-05-investigation-large-tool-result | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-06-enrichment-create-note | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-07-enrichment-log-activity-last-touch | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-08-enrichment-stage-advance-proposal | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-09-enrichment-value-and-close-date-proposal | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-10-enrichment-update-account-fields | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-11-denial-out-of-book-target | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-12-denial-nonexistent-target | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-13-denial-cross-account-comparison | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-14-denial-departed-contact-update | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-15-denial-service-principal-no-implicit-visibility | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-16-denial-tool-failure-not-conflated | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-17-lookup-opportunity-detail | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-18-lookup-account-contacts | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-19-lookup-pipeline-aggregate | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-20-lookup-proposal-queue | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-21-enrichment-pd04-multi-field-proposal | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-22-investigation-pd05-report-only | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-23-investigation-pd06-ambiguous-contact | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-24-enrichment-pd07-multi-step-write | 10 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
