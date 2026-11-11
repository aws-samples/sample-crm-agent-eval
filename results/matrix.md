# Model Evaluation Matrix

## Tier comparison summary

Per-tier figures for each arm, one column per arm in run order; the notes below the tables define each row.

### Supervisor

| Metric | anthropic | openai |
| --- | --- | --- |
| Model | anthropic.claude-sonnet-5-5 | openai.gpt-6.1-sol |
| Effort | medium | medium |
| Task completion rate | 0.941667 (113/120) | 0.991667 (119/120) |
| Task completion over repeats | 0.958333 (23/24, majority of 5) | 1.000000 (24/24, majority of 5) |
| Task accuracy | 0.984167 | 0.997500 |
| Cost per completed task | 0.011594 | 0.005498 |
| P95 latency per completed task (ms) | 10541.749459 (n=110) | 10262.295082 (n=114) |
| P50 latency per completed task (ms) | 4630.674541 (n=110) | 4112.333292 (n=114) |
| Model invocations | 273 | 242 |
| Long-context invocations | 0 | 0 |
| Input tokens | 350986 | 727458 |
| Output tokens | 47818 | 23719 |
| Cost per task, all tasks | 0.010917 | 0.005452 |
| Self-corrected retries | 0 | 0 |
| Budget-exhausted tasks | 5 | 5 |

### Subagent

| Metric | anthropic | openai |
| --- | --- | --- |
| Model | anthropic.claude-sonnet-5-5 | openai.gpt-6.1-terra |
| Effort | low | low |
| Task completion rate | 1.000000 (115/115) | 1.000000 (115/115) |
| Task completion over repeats | 1.000000 (23/23, majority of 5) | 1.000000 (23/23, majority of 5) |
| Task accuracy | 1.000000 | 1.000000 |
| Cost per completed task | 0.013680 | 0.007523 |
| P95 latency per completed task (ms) | 22778.526041 (n=55) | 14190.514624 (n=68) |
| P50 latency per completed task (ms) | 5047.331292 (n=55) | 2678.756750 (n=68) |
| Model invocations | 128 | 195 |
| Long-context invocations | 0 | 0 |
| Input tokens | 532299 | 1873711 |
| Output tokens | 41494 | 29117 |
| Cost per task, all tasks | 0.013110 | 0.007210 |
| Self-corrected retries | 0 | 0 |
| Delegation outcome: data | 95 | 95 |
| Delegation outcome: no_match | 5 | 5 |
| Delegation outcome: denied | 15 | 15 |
| Delegation outcome: tool_failure | 5 | 5 |
| Delegation outcome: none | 0 | 0 |

### ToolCall

| Metric | anthropic | openai |
| --- | --- | --- |
| Model | anthropic.claude-haiku-5-5 | openai.gpt-6-luna |
| Effort | low | low |
| Task completion rate | 1.000000 (95/95) | 1.000000 (95/95) |
| Task completion over repeats | 1.000000 (19/19, majority of 5) | 1.000000 (19/19, majority of 5) |
| Task accuracy | 1.000000 | 1.000000 |
| Cost per completed task | 0.000331 | 0.000184 |
| P95 latency per completed task (ms) | 61764.406877 (n=55) | 32371.663457 (n=63) |
| P50 latency per completed task (ms) | 2117.928416 (n=55) | 1500.936292 (n=63) |
| Model invocations | 240 | 336 |
| Long-context invocations | 0 | 0 |
| Input tokens | 159249 | 72244 |
| Output tokens | 31113 | 20463 |
| Cost per task, all tasks | 0.000262 | 0.000145 |
| Self-corrected retries | not applicable | not applicable |
| Truncated tasks | 0 | 0 |

Notes:

- Model lists the tier's configured model and every model that spent tokens at the tier; Effort lists the efforts the tier's turns ran at, with unsupported for a model that takes no effort setting. Task completion rate is rate (met/applicable) over the arm's tasks applicable at the tier, where n is the applicable task count: at Supervisor a task's correctness is 1.0, at Subagent the observed delegation outcome equals the expected one, and at ToolCall an expected tool was called with schema-valid arguments. Task accuracy is the arm's mean_correctness at Supervisor and the completion rate at Subagent and ToolCall. Task completion over repeats is rate (completed/applicable) over the arm's tasks applicable at the tier in any repeat, where a task counts when its tier expectation was met in a strict majority of its applicable repeats, and N is the per-task repeat count. Cost per completed task divides the tier's total cost by its completed task count, so spend on tasks that did not complete is included; Cost per task, all tasks divides the same total by every task the arm ran. Latency per completed task sums the tier's measured call latencies within each completed task and takes the nearest-rank percentile over those tasks; a completed task with no measured call at the tier contributes no sample, and n counts the contributing tasks. Model invocations sums the tier's included invocations; Long-context invocations counts those priced at the model's long-context rates, and reads — for a run recorded before that count existed. Self-corrected retries counts the tier's tool calls whose tool_failure result was followed, later in the same task, by a call to the same tool from the same role that returned data or no_match; such a call does not set the task's delegation outcome. ToolCall makes one call per invocation, so that row is not applicable there.
- Columns are the sweep's arms in run order, not sorted by any result, and no difference or ratio between columns is computed.
- Effort labels are not equivalent across families: each family declares its own effort scale (config/model_capabilities.yaml, effort_scale_comparability); see `effort` under "Reading these figures".
- Input tokens are each family's own reported count and are not like-for-like across families (token_accounting_disclosure, src/eval/matrix.py); see `input_tokens` comparability under "Reading these figures".
- Usage and cost for anthropic are a lower bound; that arm's section names the affected task runs.
- Usage and cost for openai are a lower bound; that arm's section names the affected task runs.

Measured dimensions per model arm — direct measurements only; no reference threshold or conclusion is computed or shown here.

## Reading these figures

Each figure below names one narrow measured signal. Its boundary is stated here so the artifact cannot be read past what it supports. Mechanism and evidence: src/eval/matrix.py.

- `mean_correctness` — The mean fraction of machine-checkable assertions satisfied over the tasks that were actually scored. Not a measure of end-to-end task success: an assertion naming a record that does not exist under the tenant an arm ran against lowers this figure whatever the turn did. Most of what it averages are negative guards a turn that does nothing at all satisfies, so its dynamic range is compressed well above zero and the figure is not a task-achievement rate; each arm's own positive-versus-guard composition, including the positive-assertion satisfaction rate, is stated with its aggregate figures.
- `bad_write_rate` — Reports only what a captured pre-turn baseline makes observable: a `must_not_change` assertion with no baseline is reported unscored and contributes nothing. Each arm's own basis is stated with its aggregate figures.
- `tool_call_correct_rate` — Reports that each scored turn issued a tool call which dispatched against an in-scope target. It is derived from exactly three conditions — a turn that issued no tool call at all, a proposal submitted on a task whose proposal limit is zero, and a proposal targeting a record other than the expected one — and from nothing else. It therefore does not report whether the turn chose the most apt tool for the task; where a task names the retrieval its answer requires, an `expected_reads` assertion checks that at least one call to a named tool reached the named record, and contributes to `mean_correctness` and deliberately not to this figure. An entry whose `target_id` is null checks only that one of the named tools was called at all, never that any particular record was reached, because the tools it names take no record id. Read to its exact boundary: it is satisfied incidentally by a turn that calls the named tool on the named record among many other calls, it says nothing about what the turn then reported, and it is therefore a necessary condition for an apt retrieval and never a sufficient one (src/eval/scoring.py, `_check_expected_reads`).
- report quality — Not measured anywhere in this project, by any figure in this artifact. Two tasks score retrieval in place of report quality: grading a multi-part prose report needs a judge model, which this project forbids, so their assertions check that the turn read the records the answer requires and nothing checks what the turn then wrote. A turn that reads exactly the right records and then reports them incompletely, or wrongly, still satisfies those assertions and still counts toward `mean_correctness`. The `lookup` tasks' positive signal is retrieval on the same terms, and for two of the four it is weaker still: their `expected_reads` entry carries a null `target_id`, so the satisfied assertion evidences only that the named whole-book tool was called at all, not that any particular record was read. The narrowing was authorised deliberately and each narrowed task's own `notes` in golden_tasks.yaml restate the superseded reasoning at length. The tasks narrowed to retrieval are: `gt-04-investigation-turn-budget-partial`, `gt-05-investigation-large-tool-result`.
- retrieval narrowing — A task narrowed to retrieval names the specific retrieval its assertions check, and names only the retrievals that expose an observable target id. A turn that answers such a task by an equally apt retrieval which exposes no target id therefore satisfies none of that task's positive assertions, for a reason unrelated to what the model did. The deduction that conservatism makes to `mean_correctness` is quantified per arm with that arm's aggregate figures, as an upper bound derived from the arm's own per-task records, so a reader of this artifact alone can size it against the figures it appears beside.
- Retrieval-narrowing magnitude: the largest deduction any one arm's retrieval-narrowed tasks account for is 0.000000 of that arm's `mean_correctness`, which is smaller than the largest absolute difference between any two arms' `mean_correctness` in this sweep (0.013333). Which arm sits on which side of that difference is deliberately not computed or stated anywhere in this artifact; the comparison is published because a disclosed measurement conservatism of this size bounds how much the difference between arms can be read as a property of the models (src/eval/golden_tasks.yaml).
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
| timestamp | 2026-11-10T20:18:32.528771+00:00 |
| model_id | anthropic.claude-sonnet-5-5 |
| effort | medium |
| dataset_seed | 20260101 |
| runtime_identifier | live-aws-lambda-dispatch |
| price_table_version | 1.3.0 |
| answer_style_sha256 | 953efa6d0d26da1fb09cf2cdf625c78f0bd8f7d04da7652e581e623edea67f47 |
| prompt_bundle_sha256 | 2959cde8d2bde46cfdf198614a890110d40d4753501f6151fb49f59285e3cec1 |

### Usage and Cost

Supervisor tier only: these figures cover the arm's supervisor-tier calls to anthropic.claude-sonnet-5-5.

| field | value |
| --- | --- |
| model_id | anthropic.claude-sonnet-5-5 |
| included_invocation_count | 273 |
| long_context_invocation_count | 0 |
| input_tokens | 350986 |
| output_tokens | 47818 |
| cache_write_tokens | 4375 |
| cache_read_tokens | 1190000 |
| cache_min_checkpoint_tokens | 512 |
| latency_measured | yes |
| latency_percentile_method | nearest-rank |
| task_latency_ms_p50 | 7596.169667 |
| task_latency_ms_p95 | 24924.877333 |
| cost_withheld_provisional | no |
| cost_input | 0.701972 |
| cost_output | 0.478180 |
| cost_cache_write | 0.010938 |
| cost_cache_read | 0.119000 |
| cost_excluding_cache | 1.180152 |
| cost_cache_total | 0.129938 |
| cost_total | 1.310090 |
| price_table_version | 1.3.0 |

All tiers (every call the arm made):

| field | value |
| --- | --- |
| arm_cost_total | 2.914799 |
| arm_long_context_invocation_count | 0 |

Usage and cost for this arm are a lower bound: in 5 task run(s) (gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial) a model call ended before reporting token usage, so its tokens and cost are not counted in the figures above.

Call latency by tier (ms):

| tier | p50 | p95 | count |
| --- | --- | --- | --- |
| supervisor | 1803.318791 | 4399.364166 | 273 |
| subagent | 2267.182459 | 7086.334042 | 128 |
| tool | 1576.261125 | 2854.407583 | 240 |

Per tier / role / model:

| tier | role | model_id | included_invocation_count | long_context_invocation_count | input_tokens | output_tokens | cache_read_tokens | cache_write_tokens | call_latency_measured | call_latency_ms_p50 | call_latency_ms_p95 | cost_withheld_provisional | cost_input | cost_output | cost_cache_read | cost_cache_write | cost_excluding_cache | cost_cache_total | cost_total | price_table_version |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| subagent | mutation | anthropic.claude-sonnet-5-5 | 105 | 0 | 94965 | 23596 | 674544 | 6486 | yes | 2150.364500 | 4162.445542 | no | 0.189930 | 0.235960 | 0.067454 | 0.016215 | 0.425890 | 0.083669 | 0.509559 | 1.3.0 |
| subagent | research | anthropic.claude-sonnet-5-5 | 23 | 0 | 437334 | 17898 | 46904 | 2132 | yes | 5047.587833 | 11582.232958 | no | 0.874668 | 0.178980 | 0.004690 | 0.005330 | 1.053648 | 0.010020 | 1.063668 | 1.3.0 |
| supervisor | supervisor | anthropic.claude-sonnet-5-5 | 273 | 0 | 350986 | 47818 | 1190000 | 4375 | yes | 1803.318791 | 4399.364166 | no | 0.701972 | 0.478180 | 0.119000 | 0.010938 | 1.180152 | 0.129937 | 1.310090 | 1.3.0 |
| tool | mutation | anthropic.claude-haiku-5-5 | 62 | 0 | 58185 | 15131 | 0 | 0 | yes | 1906.717834 | 2801.624333 | no | 0.005818 | 0.007565 | 0.000000 | 0.000000 | 0.013384 | 0.000000 | 0.013384 | 1.3.0 |
| tool | research | anthropic.claude-haiku-5-5 | 178 | 0 | 101064 | 15982 | 0 | 0 | yes | 1518.533375 | 2929.801709 | no | 0.010106 | 0.007991 | 0.000000 | 0.000000 | 0.018097 | 0.000000 | 0.018097 | 1.3.0 |

Per-tier summary:

| tier | input_tokens | output_tokens | cost | latency_p50_ms | latency_p95_ms | success (met/applicable) | success_rate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| supervisor | 350986 | 47818 | 1.310090 | 1803.318791 | 4399.364166 | 113/120 | 0.941667 |
| subagent | 532299 | 41494 | 1.573228 | 2267.182459 | 7086.334042 | 115/115 | 1.000000 |
| tool | 159249 | 31113 | 0.031481 | 1576.261125 | 2854.407583 | 95/95 | 1.000000 |

Per tool:

| tool_name | role | count | call_latency_ms_p50 | call_latency_ms_p95 | truncated_count | outcome_data | outcome_no_match | outcome_denied | outcome_tool_failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| create_note | mutation | 10 | 350.102125 | 1911.414917 | 0 | 10 | 0 | 0 | 0 |
| get_account | research | 16 | 268.130375 | 295.391167 | 0 | 16 | 0 | 0 | 0 |
| get_account | supervisor | 20 | 287.268959 | 388.594375 | 0 | 20 | 0 | 0 | 0 |
| get_contact | supervisor | 23 | 258.644000 | 368.531209 | 0 | 23 | 0 | 0 | 0 |
| get_opportunity | research | 158 | 309.076709 | 369.213583 | 0 | 158 | 0 | 0 | 0 |
| get_opportunity | supervisor | 46 | 305.912291 | 359.309000 | 0 | 36 | 5 | 5 | 0 |
| list_activities | supervisor | 10 | 2.323250 | 2217.895625 | 0 | 5 | 0 | 0 | 5 |
| list_proposals | supervisor | 5 | 1999.800167 | 2408.586416 | 0 | 5 | 0 | 0 | 0 |
| log_activity | mutation | 10 | 339.626333 | 2101.602416 | 0 | 10 | 0 | 0 | 0 |
| pipeline_aggregate | supervisor | 5 | 2268.992584 | 2705.675875 | 0 | 5 | 0 | 0 | 0 |
| search_accounts | supervisor | 5 | 2101.000834 | 2280.018000 | 0 | 5 | 0 | 0 | 0 |
| search_opportunities | research | 4 | 277.645875 | 2031.635792 | 0 | 4 | 0 | 0 | 0 |
| search_opportunities | supervisor | 16 | 136.441084 | 299.789042 | 0 | 6 | 0 | 10 | 0 |
| submit_proposal | mutation | 32 | 308.653708 | 436.228375 | 0 | 32 | 0 | 0 | 0 |
| update_account | mutation | 5 | 2057.290125 | 2071.808959 | 0 | 5 | 0 | 0 | 0 |
| update_contact | mutation | 5 | 1987.479208 | 2078.091875 | 0 | 5 | 0 | 0 | 0 |

Excluded invocations: 0 total (cold_start: 0, retry: 0, throttle: 0)

### Aggregate Score

| field | value |
| --- | --- |
| scored_task_count | 120 |
| unscored_task_count | 0 |
| mean_correctness | 0.984167 |
| bad_write_rate | 0.000000 |
| tool_call_correct_rate | 1.000000 |

`bad_write_rate` basis: all 250 declared `must_not_change` assertion(s) in this arm had a captured pre-turn baseline, so the figure is a measurement over them (src/eval/matrix.py).

`mean_correctness` composition: negative guards a do-nothing turn satisfies are 0.8161 of the assertion mass this figure averages — the mean over the 120 scored tasks of each task's own guard share, weighted exactly as `mean_correctness` itself is, rather than the ratio pooled over assertions (pooled totals, for context and not the basis of the figure just quoted: 682 guard assertions and 145 positive ones across those tasks). So the figure is not a task-achievement rate. Taken alone, 140 of 145 positive assertions were satisfied (0.9655); the guards alone were satisfied at 0.9795 (src/eval/matrix.py).

Retrieval-narrowed task deduction: the assertions this arm did not satisfy on the tasks narrowed to retrieval account for at most 0.000000 of its `mean_correctness` (`gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000), an upper bound over the 120 scored tasks derived from this arm's own per-task records. The figure itself is unchanged by this disclosure (src/eval/golden_tasks.yaml).

### Per-Task Results

| task_id | scoring_mode | correctness | unscored_reason | bad_write | tool_call_correct | truncated | budget_exhausted |
| --- | --- | --- | --- | --- | --- | --- | --- |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | no | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | no | yes |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | no | no |
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
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | no | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 0.700000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | no | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | no | yes |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | no | no |
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
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | no | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 0.700000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | no | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | no | yes |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | no | no |
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
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | no | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 0.700000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | no | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | no | yes |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | no | no |
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
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | no | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 0.700000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | no | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | no | yes |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | no | no |
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
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | no | no |
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
| gt-02-investigation-pd02-stale-forecast | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-03-investigation-pd03-scoped-lookup | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-04-investigation-turn-budget-partial | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-05-investigation-large-tool-result | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-06-enrichment-create-note | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-07-enrichment-log-activity-last-touch | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-08-enrichment-stage-advance-proposal | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-09-enrichment-value-and-close-date-proposal | 5 | 0.800000 | 0.920000 | 1.000000 | 0.600000 |
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
| timestamp | 2026-11-10T20:40:59.870904+00:00 |
| model_id | openai.gpt-6.1-sol |
| effort | medium |
| dataset_seed | 20260101 |
| runtime_identifier | live-aws-lambda-dispatch |
| price_table_version | 1.3.0 |
| answer_style_sha256 | 953efa6d0d26da1fb09cf2cdf625c78f0bd8f7d04da7652e581e623edea67f47 |
| prompt_bundle_sha256 | 2959cde8d2bde46cfdf198614a890110d40d4753501f6151fb49f59285e3cec1 |

### Usage and Cost

Supervisor tier only: these figures cover the arm's supervisor-tier calls to openai.gpt-6.1-sol.

| field | value |
| --- | --- |
| model_id | openai.gpt-6.1-sol |
| included_invocation_count | 242 |
| long_context_invocation_count | 0 |
| input_tokens | 727458 |
| output_tokens | 23719 |
| cache_write_tokens | 143097 |
| cache_read_tokens | 583877 |
| cache_min_checkpoint_tokens | 1024 |
| latency_measured | yes |
| latency_percentile_method | nearest-rank |
| task_latency_ms_p50 | 7513.681541 |
| task_latency_ms_p95 | 29048.906041 |
| cost_withheld_provisional | no |
| cost_input | 0.000968 |
| cost_output | 0.237190 |
| cost_cache_write | 0.357743 |
| cost_cache_read | 0.058388 |
| cost_excluding_cache | 0.238158 |
| cost_cache_total | 0.416130 |
| cost_total | 0.654288 |
| price_table_version | 1.3.0 |

All tiers (every call the arm made):

| field | value |
| --- | --- |
| arm_cost_total | 1.536942 |
| arm_long_context_invocation_count | 0 |

Usage and cost for this arm are a lower bound: in 5 task run(s) (gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial) a model call ended before reporting token usage, so its tokens and cost are not counted in the figures above.

Call latency by tier (ms):

| tier | p50 | p95 | count |
| --- | --- | --- | --- |
| supervisor | 2064.151709 | 3983.399916 | 242 |
| subagent | 1081.820291 | 3070.852542 | 195 |
| tool | 724.084167 | 1479.331417 | 336 |

Per tier / role / model:

| tier | role | model_id | included_invocation_count | long_context_invocation_count | input_tokens | output_tokens | cache_read_tokens | cache_write_tokens | call_latency_measured | call_latency_ms_p50 | call_latency_ms_p95 | cost_withheld_provisional | cost_input | cost_output | cost_cache_read | cost_cache_write | cost_excluding_cache | cost_cache_total | cost_total | price_table_version |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| subagent | analytics | openai.gpt-6.1-terra | 10 | 0 | 21757 | 1174 | 5366 | 16371 | yes | 1081.820291 | 1867.854583 | no | 0.000020 | 0.005870 | 0.000268 | 0.020464 | 0.005890 | 0.020732 | 0.026622 | 1.3.0 |
| subagent | mutation | openai.gpt-6.1-terra | 135 | 0 | 543002 | 11611 | 468729 | 74003 | yes | 964.570000 | 1789.223250 | no | 0.000270 | 0.058055 | 0.023436 | 0.092504 | 0.058325 | 0.115940 | 0.174265 | 1.3.0 |
| subagent | research | openai.gpt-6.1-terra | 50 | 0 | 1308952 | 16332 | 877929 | 430923 | yes | 1371.243291 | 7899.265125 | no | 0.000100 | 0.081660 | 0.043896 | 0.538654 | 0.081760 | 0.582550 | 0.664310 | 1.3.0 |
| supervisor | supervisor | openai.gpt-6.1-sol | 242 | 0 | 727458 | 23719 | 583877 | 143097 | yes | 2064.151709 | 3983.399916 | no | 0.000968 | 0.237190 | 0.058388 | 0.357743 | 0.238158 | 0.416130 | 0.654288 | 1.3.0 |
| tool | analytics | openai.gpt-6-luna | 10 | 0 | 3270 | 490 | 0 | 0 | yes | 694.980750 | 1042.591792 | no | 0.000327 | 0.000245 | 0.000000 | 0.000000 | 0.000572 | 0.000000 | 0.000572 | 1.3.0 |
| tool | mutation | openai.gpt-6-luna | 80 | 0 | 26761 | 7867 | 0 | 0 | yes | 969.241042 | 1678.421334 | no | 0.002676 | 0.003934 | 0.000000 | 0.000000 | 0.006610 | 0.000000 | 0.006610 | 1.3.0 |
| tool | research | openai.gpt-6-luna | 246 | 0 | 42213 | 12106 | 0 | 0 | yes | 699.093875 | 1096.752833 | no | 0.004221 | 0.006053 | 0.000000 | 0.000000 | 0.010274 | 0.000000 | 0.010274 | 1.3.0 |

Per-tier summary:

| tier | input_tokens | output_tokens | cost | latency_p50_ms | latency_p95_ms | success (met/applicable) | success_rate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| supervisor | 727458 | 23719 | 0.654288 | 2064.151709 | 3983.399916 | 119/120 | 0.991667 |
| subagent | 1873711 | 29117 | 0.865197 | 1081.820291 | 3070.852542 | 115/115 | 1.000000 |
| tool | 72244 | 20463 | 0.017456 | 724.084167 | 1479.331417 | 95/95 | 1.000000 |

Per tool:

| tool_name | role | count | call_latency_ms_p50 | call_latency_ms_p95 | truncated_count | outcome_data | outcome_no_match | outcome_denied | outcome_tool_failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| create_note | mutation | 10 | 323.062041 | 394.156750 | 0 | 10 | 0 | 0 | 0 |
| get_account | mutation | 10 | 274.082500 | 389.635750 | 0 | 10 | 0 | 0 | 0 |
| get_account | research | 10 | 267.483959 | 292.097791 | 0 | 10 | 0 | 0 | 0 |
| get_account | supervisor | 5 | 296.824750 | 320.795250 | 0 | 5 | 0 | 0 | 0 |
| get_contact | research | 26 | 247.368958 | 294.216417 | 0 | 26 | 0 | 0 | 0 |
| get_contact | supervisor | 5 | 250.503333 | 273.101208 | 0 | 5 | 0 | 0 | 0 |
| get_opportunity | mutation | 6 | 303.956875 | 395.329333 | 0 | 6 | 0 | 0 | 0 |
| get_opportunity | research | 196 | 312.354917 | 414.298959 | 0 | 196 | 0 | 0 | 0 |
| get_opportunity | supervisor | 25 | 305.323792 | 412.832458 | 0 | 15 | 5 | 5 | 0 |
| list_activities | research | 8 | 280.460500 | 511.758250 | 0 | 8 | 0 | 0 | 0 |
| list_activities | supervisor | 8 | 1.360375 | 306.790416 | 0 | 3 | 0 | 0 | 5 |
| list_audit_records | mutation | 2 | 252.085416 | 1976.990250 | 0 | 2 | 0 | 0 | 0 |
| list_audit_records | research | 1 | 1624.517250 | 1624.517250 | 0 | 1 | 0 | 0 | 0 |
| list_proposals | mutation | 1 | 278.802750 | 278.802750 | 0 | 1 | 0 | 0 | 0 |
| list_proposals | supervisor | 5 | 328.874916 | 411.687750 | 0 | 5 | 0 | 0 | 0 |
| log_activity | mutation | 10 | 336.799833 | 400.390917 | 0 | 10 | 0 | 0 | 0 |
| pipeline_aggregate | supervisor | 5 | 499.190875 | 552.098542 | 0 | 5 | 0 | 0 | 0 |
| search_accounts | mutation | 5 | 315.369167 | 1987.302333 | 0 | 5 | 0 | 0 | 0 |
| search_opportunities | analytics | 10 | 125.000666 | 284.981083 | 0 | 5 | 0 | 5 | 0 |
| search_opportunities | research | 5 | 278.005416 | 301.715834 | 0 | 5 | 0 | 0 | 0 |
| search_opportunities | supervisor | 5 | 120.459958 | 130.287458 | 0 | 0 | 0 | 5 | 0 |
| submit_proposal | mutation | 26 | 315.181833 | 352.988250 | 0 | 26 | 0 | 0 | 0 |
| update_account | mutation | 5 | 346.125458 | 2066.207083 | 0 | 5 | 0 | 0 | 0 |
| update_contact | mutation | 5 | 327.592000 | 1943.571542 | 0 | 5 | 0 | 0 | 0 |

Excluded invocations: 0 total (cold_start: 0, retry: 0, throttle: 0)

### Aggregate Score

| field | value |
| --- | --- |
| scored_task_count | 120 |
| unscored_task_count | 0 |
| mean_correctness | 0.997500 |
| bad_write_rate | 0.000000 |
| tool_call_correct_rate | 1.000000 |

`bad_write_rate` basis: all 250 declared `must_not_change` assertion(s) in this arm had a captured pre-turn baseline, so the figure is a measurement over them (src/eval/matrix.py).

`mean_correctness` composition: negative guards a do-nothing turn satisfies are 0.8155 of the assertion mass this figure averages — the mean over the 120 scored tasks of each task's own guard share, weighted exactly as `mean_correctness` itself is, rather than the ratio pooled over assertions (pooled totals, for context and not the basis of the figure just quoted: 676 guard assertions and 145 positive ones across those tasks). So the figure is not a task-achievement rate. Taken alone, 144 of 145 positive assertions were satisfied (0.9931); the guards alone were satisfied at 0.9970 (src/eval/matrix.py).

Retrieval-narrowed task deduction: the assertions this arm did not satisfy on the tasks narrowed to retrieval account for at most 0.000000 of its `mean_correctness` (`gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000; `gt-04-investigation-turn-budget-partial` scored 1.000000, at most 0.000000; `gt-05-investigation-large-tool-result` scored 1.000000, at most 0.000000), an upper bound over the 120 scored tasks derived from this arm's own per-task records. The figure itself is unchanged by this disclosure (src/eval/golden_tasks.yaml).

### Per-Task Results

| task_id | scoring_mode | correctness | unscored_reason | bad_write | tool_call_correct | truncated | budget_exhausted |
| --- | --- | --- | --- | --- | --- | --- | --- |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | no | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | no | yes |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | no | no |
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
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | no | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | no | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | no | yes |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | no | no |
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
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | no | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 0.700000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | no | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | no | yes |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | no | no |
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
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | no | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | no | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | no | yes |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | no | no |
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
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | no | no |
| gt-18-lookup-account-contacts | machine | 1.000000 | — | no | yes | no | no |
| gt-19-lookup-pipeline-aggregate | machine | 1.000000 | — | no | yes | no | no |
| gt-20-lookup-proposal-queue | machine | 1.000000 | — | no | yes | no | no |
| gt-21-enrichment-pd04-multi-field-proposal | machine | 1.000000 | — | no | yes | no | no |
| gt-22-investigation-pd05-report-only | machine | 1.000000 | — | no | yes | no | no |
| gt-23-investigation-pd06-ambiguous-contact | machine | 1.000000 | — | no | yes | no | no |
| gt-24-enrichment-pd07-multi-step-write | machine | 1.000000 | — | no | yes | no | no |
| gt-01-investigation-pd01-successor | machine | 1.000000 | — | no | yes | no | no |
| gt-02-investigation-pd02-stale-forecast | machine | 1.000000 | — | no | yes | no | no |
| gt-03-investigation-pd03-scoped-lookup | machine | 1.000000 | — | no | yes | no | no |
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | no | yes |
| gt-05-investigation-large-tool-result | machine | 1.000000 | — | no | yes | no | no |
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
| gt-17-lookup-opportunity-detail | machine | 1.000000 | — | no | yes | no | no |
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
| gt-01-investigation-pd01-successor | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
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
| gt-21-enrichment-pd04-multi-field-proposal | 5 | 0.700000 | 0.940000 | 1.000000 | 0.800000 |
| gt-22-investigation-pd05-report-only | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-23-investigation-pd06-ambiguous-contact | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-24-enrichment-pd07-multi-step-write | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
