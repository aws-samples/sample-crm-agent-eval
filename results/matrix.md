# Model Evaluation Matrix

## Tier comparison summary

Per-tier figures for each arm, one column per arm in run order; the notes below the tables define each row.

### Supervisor

| Metric | anthropic | openai |
| --- | --- | --- |
| Model | anthropic.claude-sonnet-5-5 | openai.gpt-6.1-sol |
| Effort | medium | medium |
| Task completion rate | 0.933333 (112/120) | 1.000000 (120/120) |
| Task completion over repeats | 0.958333 (23/24, majority of 5) | 1.000000 (24/24, majority of 5) |
| Task accuracy | 0.982130 | 1.000000 |
| Cost per completed task | 0.013337 | 0.005343 |
| P95 latency per completed task (ms) | 11197.106291 (n=111) | 11562.644292 (n=115) |
| P50 latency per completed task (ms) | 4035.949583 (n=111) | 4684.243208 (n=115) |
| Model invocations | 281 | 242 |
| Input tokens | 437073 | 692274 |
| Output tokens | 49150 | 23443 |
| Cost per task, all tasks | 0.012448 | 0.005343 |
| Self-corrected retries | 0 | 0 |
| Budget-exhausted tasks | 4 | 5 |

### Subagent

| Metric | anthropic | openai |
| --- | --- | --- |
| Model | anthropic.claude-sonnet-5-5 | openai.gpt-6.1-terra |
| Effort | low | low |
| Task completion rate | 1.000000 (115/115) | 1.000000 (115/115) |
| Task completion over repeats | 1.000000 (23/23, majority of 5) | 1.000000 (23/23, majority of 5) |
| Task accuracy | 1.000000 | 1.000000 |
| Cost per completed task | 0.015107 | 0.007416 |
| P95 latency per completed task (ms) | 32948.108208 (n=54) | 12691.750541 (n=65) |
| P50 latency per completed task (ms) | 4681.970250 (n=54) | 2412.751916 (n=65) |
| Model invocations | 124 | 193 |
| Input tokens | 558126 | 1760204 |
| Output tokens | 52672 | 28510 |
| Cost per task, all tasks | 0.014478 | 0.007107 |
| Self-corrected retries | 3 | 0 |
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
| Cost per completed task | 0.000323 | 0.000176 |
| P95 latency per completed task (ms) | 46802.357543 (n=54) | 30112.864834 (n=60) |
| P50 latency per completed task (ms) | 1909.853791 (n=54) | 1337.665750 (n=60) |
| Model invocations | 235 | 336 |
| Input tokens | 154440 | 68109 |
| Output tokens | 30555 | 19848 |
| Cost per task, all tasks | 0.000256 | 0.000139 |
| Self-corrected retries | not applicable | not applicable |
| Truncated tasks | 0 | 0 |

Notes:

- Model lists the tier's configured model and every model that spent tokens at the tier; Effort lists the efforts the tier's turns ran at, with unsupported for a model that takes no effort setting. Task completion rate is rate (met/applicable) over the arm's tasks applicable at the tier, where n is the applicable task count: at Supervisor a task's correctness is 1.0, at Subagent the observed delegation outcome equals the expected one, and at ToolCall an expected tool was called with schema-valid arguments. Task accuracy is the arm's mean_correctness at Supervisor and the completion rate at Subagent and ToolCall. Task completion over repeats is rate (completed/applicable) over the arm's tasks applicable at the tier in any repeat, where a task counts when its tier expectation was met in a strict majority of its applicable repeats, and N is the per-task repeat count. Cost per completed task divides the tier's total cost by its completed task count, so spend on tasks that did not complete is included; Cost per task, all tasks divides the same total by every task the arm ran. Latency per completed task sums the tier's measured call latencies within each completed task and takes the nearest-rank percentile over those tasks; a completed task with no measured call at the tier contributes no sample, and n counts the contributing tasks. Model invocations sums the tier's included invocations. Self-corrected retries counts the tier's tool calls whose tool_failure result was followed, later in the same task, by a call to the same tool from the same role that returned data or no_match; such a call does not set the task's delegation outcome. ToolCall makes one call per invocation, so that row is not applicable there.
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
- Retrieval-narrowing magnitude: the largest deduction any one arm's retrieval-narrowed tasks account for is 0.000000 of that arm's `mean_correctness`, which is smaller than the largest absolute difference between any two arms' `mean_correctness` in this sweep (0.017870). Which arm sits on which side of that difference is deliberately not computed or stated anywhere in this artifact; the comparison is published because a disclosed measurement conservatism of this size bounds how much the difference between arms can be read as a property of the models (src/eval/golden_tasks.yaml).
- `effort` (provenance) — The reasoning-effort string sent to each arm's supervisor tier, pinned identically across arms so the sweep is reproducible; the subagent and tool tiers run their config/models.yaml tier effort. It is not a claim that the arms were given equivalent reasoning budgets: each family declares its own effort scale with its own rung count and its own default, so an identical label sits at a different position on each family's scale. The declared scales, and this non-equivalence, are recorded in config/model_capabilities.yaml under `effort_scale_comparability.compared_models`, per tier. Sweeps recorded before tier efforts were configured ran the subagent and tool tiers at each model's own default effort (the Anthropic subagent at `high`), so their subagent and tool tiers are not effort-comparable with this artifact's.
- `input_tokens` comparability — Each arm's `input_tokens` is the count its own provider family reported, and the families report cached prompt reads in differently-shaped usage objects: one as a sibling of `input_tokens`, the other nested inside its breakdown. Whether a family's `input_tokens` already contains the `cache_read_tokens` printed beside it is therefore a per-family question, and this project declares it per family with the evidence behind the declaration rather than assuming the two figures count the same thing. Two consequences, both exact: the arms' `input_tokens` are not established to be like-for-like totals and should not be differenced or ratioed as though they were; and while any family's accounting is declared unconfirmed, pricing refuses to produce a publishable currency figure for it at all, so no cost comparison can rest on the ambiguity. No token figure in this artifact is adjusted by this disclosure — each is the count that was reported. (src/crm/metering.py)
  - `anthropic`: cached reads read from `usage.cache_read_input_tokens (Messages API message_start)`; accounting priced under: `exclusive`; direction the evidence points: `exclusive`. Exclusive (disjoint): inputTokens excludes both cacheReadInputTokens and cacheWriteInputTokens; the total input is their sum. AWS's own Bedrock prompt-caching walkthrough shows a cache-read response as {"inputTokens": 28, "outputTokens": 294, "cacheWriteInputTokens": 0, "cacheReadInputTokens": 1898} and states "Only 28 tokens (the question itself) were processed as standard input" while the 1,898 reused tokens are reported separately in cacheReadInputTokens — so the cached count is not inside inputTokens. Source: https://aws.amazon.com/blogs/machine-learning/optimizing-cost-and-latency-with-amazon-bedrock-prompt-caching/ (verified 2026-09-28). The Messages API message_start path the adapter reads carries the same disjoint cache_read_input_tokens / cache_creation_input_tokens siblings.
  - `openai`: cached reads read from `usage.input_tokens_details.cached_tokens (Responses API response.completed)`; accounting priced under: `inclusive`; direction the evidence points: `inclusive`. Inclusive (subset): input_tokens is the full input count and both the cached reads and the cache writes are part of it, not added on top. AWS's Bedrock explicit-caching documentation for the OpenAI models states verbatim "input_tokens = cached_tokens + cache_write_tokens + non-cached remainder" and "Because the cached and written counts are part of input_tokens rather than added on top of it, a token served from the cache is counted once and billed once at the cache-read rate. You are never charged twice for the same token." Its worked example confirms the arithmetic (input_tokens 3682 = cached 0 + cache_write 3626 + new 56). Source: https://aws.amazon.com/blogs/machine-learning/introducing-explicit-prompt-caching-for-openai-gpt-5-6-models-on-amazon-bedrock/ (verified 2026-09-28). So both cached_tokens and cache_write_tokens must be subtracted from input_tokens to obtain the tokens billed at the standard input rate. CAVEAT: that AWS source describes the GPT-5.6 models; the OpenAI models in the matrix use the same Responses API usage shape (input_tokens with input_tokens_details.cached_tokens), and no vendor statement of this accounting specific to them was found, so the reading is carried forward by shape. The first --live sweep that records a non-zero cached read should be reconciled against the actual Bedrock bill to confirm it holds for them.

## anthropic.claude-sonnet-5-5 (live-mode run)

### Provenance

| field | value |
| --- | --- |
| mode | live |
| timestamp | 2026-10-29T03:39:53.444033+00:00 |
| model_id | anthropic.claude-sonnet-5-5 |
| effort | medium |
| dataset_seed | 20260101 |
| runtime_identifier | live-aws-lambda-dispatch |
| price_table_version | 1.2.0 |
| answer_style_sha256 | 953efa6d0d26da1fb09cf2cdf625c78f0bd8f7d04da7652e581e623edea67f47 |
| prompt_bundle_sha256 | 77723af1ff2c9b216e100c7b352e9729dc4063fe2edd0bd7f3c12754fdb1ce1d |

### Usage and Cost

| field | value |
| --- | --- |
| model_id | anthropic.claude-sonnet-5-5 |
| included_invocation_count | 119 |
| input_tokens | 437073 |
| output_tokens | 49150 |
| cache_write_tokens | 4199 |
| cache_read_tokens | 1175720 |
| cache_min_checkpoint_tokens | 512 |
| latency_measured | yes |
| latency_percentile_method | nearest-rank |
| task_latency_ms_p50 | 6724.061625 |
| task_latency_ms_p95 | 26708.118958 |
| cost_withheld_provisional | no |
| cost_input | 0.874146 |
| cost_output | 0.491500 |
| cost_cache_write | 0.010497 |
| cost_cache_read | 0.117572 |
| cost_excluding_cache | 1.365646 |
| cost_cache_total | 0.128070 |
| cost_total | 1.493715 |
| price_table_version | 1.2.0 |

Usage and cost for this arm are a lower bound: in 4 task run(s) (gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial) a model call ended before reporting token usage, so its tokens and cost are not counted in the figures above.

Call latency by tier (ms):

| tier | p50 | p95 | count |
| --- | --- | --- | --- |
| supervisor | 1679.674291 | 3767.128250 | 281 |
| subagent | 2124.368833 | 15145.678916 | 124 |
| tool | 1160.205458 | 2030.019042 | 235 |

Per tier / role / model:

| tier | role | model_id | included_invocation_count | input_tokens | output_tokens | cache_read_tokens | cache_write_tokens | call_latency_measured | call_latency_ms_p50 | call_latency_ms_p95 | cost_withheld_provisional | cost_input | cost_output | cost_cache_read | cost_cache_write | cost_excluding_cache | cost_cache_total | cost_total | price_table_version |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| subagent | mutation | anthropic.claude-sonnet-5-5 | 111 | 117986 | 25524 | 669900 | 6090 | yes | 2093.161167 | 3460.283042 | no | 0.235972 | 0.255240 | 0.066990 | 0.015225 | 0.491212 | 0.082215 | 0.573427 | 1.2.0 |
| subagent | research | anthropic.claude-sonnet-5-5 | 13 | 440140 | 27148 | 21923 | 3986 | yes | 15145.678916 | 21632.722500 | no | 0.880280 | 0.271480 | 0.002192 | 0.009965 | 1.151760 | 0.012157 | 1.163917 | 1.2.0 |
| supervisor | supervisor | anthropic.claude-sonnet-5-5 | 281 | 437073 | 49150 | 1175720 | 4199 | yes | 1679.674291 | 3767.128250 | no | 0.874146 | 0.491500 | 0.117572 | 0.010497 | 1.365646 | 0.128070 | 1.493716 | 1.2.0 |
| tool | mutation | anthropic.claude-haiku-5-5 | 69 | 62368 | 16048 | 0 | 0 | yes | 1644.373833 | 3394.509750 | no | 0.006237 | 0.008024 | 0.000000 | 0.000000 | 0.014261 | 0.000000 | 0.014261 | 1.2.0 |
| tool | research | anthropic.claude-haiku-5-5 | 166 | 92072 | 14507 | 0 | 0 | yes | 1122.135500 | 1804.872000 | no | 0.009207 | 0.007253 | 0.000000 | 0.000000 | 0.016461 | 0.000000 | 0.016461 | 1.2.0 |

Per-tier summary:

| tier | input_tokens | output_tokens | cost | latency_p50_ms | latency_p95_ms | success (met/applicable) | success_rate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| supervisor | 437073 | 49150 | 1.493716 | 1679.674291 | 3767.128250 | 112/120 | 0.933333 |
| subagent | 558126 | 52672 | 1.737344 | 2124.368833 | 15145.678916 | 115/115 | 1.000000 |
| tool | 154440 | 30555 | 0.030722 | 1160.205458 | 2030.019042 | 95/95 | 1.000000 |

Per tool:

| tool_name | role | count | call_latency_ms_p50 | call_latency_ms_p95 | truncated_count | outcome_data | outcome_no_match | outcome_denied | outcome_tool_failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| create_note | mutation | 10 | 395.405625 | 1955.169209 | 0 | 10 | 0 | 0 | 0 |
| get_account | research | 10 | 252.081541 | 330.135792 | 0 | 10 | 0 | 0 | 0 |
| get_account | supervisor | 36 | 267.047958 | 395.243083 | 0 | 36 | 0 | 0 | 0 |
| get_contact | research | 1 | 249.045417 | 249.045417 | 0 | 1 | 0 | 0 | 0 |
| get_contact | supervisor | 23 | 249.728875 | 351.584416 | 0 | 23 | 0 | 0 | 0 |
| get_opportunity | research | 152 | 301.124041 | 402.877000 | 0 | 152 | 0 | 0 | 0 |
| get_opportunity | supervisor | 47 | 313.865375 | 435.351666 | 0 | 37 | 5 | 5 | 0 |
| list_activities | supervisor | 10 | 0.647250 | 2009.227291 | 0 | 5 | 0 | 0 | 5 |
| list_proposals | mutation | 3 | 267.774708 | 268.921833 | 0 | 3 | 0 | 0 | 0 |
| list_proposals | supervisor | 5 | 311.241542 | 1960.959333 | 0 | 5 | 0 | 0 | 0 |
| log_activity | mutation | 10 | 391.168458 | 1942.776875 | 0 | 10 | 0 | 0 | 0 |
| pipeline_aggregate | supervisor | 5 | 1090.526916 | 2696.057917 | 0 | 5 | 0 | 0 | 0 |
| search_accounts | supervisor | 5 | 435.152875 | 1877.212750 | 0 | 5 | 0 | 0 | 0 |
| search_opportunities | research | 3 | 268.024333 | 292.754000 | 0 | 3 | 0 | 0 | 0 |
| search_opportunities | supervisor | 19 | 126.470875 | 413.476084 | 0 | 9 | 0 | 10 | 0 |
| submit_proposal | mutation | 36 | 308.012167 | 403.327958 | 0 | 33 | 0 | 0 | 3 |
| update_account | mutation | 5 | 1921.012375 | 2059.647458 | 0 | 5 | 0 | 0 | 0 |
| update_contact | mutation | 5 | 394.912625 | 2011.498792 | 0 | 5 | 0 | 0 | 0 |

Excluded invocations: 0 total (cold_start: 0, retry: 0, throttle: 0)

### Aggregate Score

| field | value |
| --- | --- |
| scored_task_count | 120 |
| unscored_task_count | 0 |
| mean_correctness | 0.982130 |
| bad_write_rate | 0.000000 |
| tool_call_correct_rate | 1.000000 |

`bad_write_rate` basis: all 250 declared `must_not_change` assertion(s) in this arm had a captured pre-turn baseline, so the figure is a measurement over them (src/eval/matrix.py).

`mean_correctness` composition: negative guards a do-nothing turn satisfies are 0.8162 of the assertion mass this figure averages — the mean over the 120 scored tasks of each task's own guard share, weighted exactly as `mean_correctness` itself is, rather than the ratio pooled over assertions (pooled totals, for context and not the basis of the figure just quoted: 683 guard assertions and 145 positive ones across those tasks). So the figure is not a task-achievement rate. Taken alone, 138 of 145 positive assertions were satisfied (0.9517); the guards alone were satisfied at 0.9795 (src/eval/matrix.py).

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
| gt-02-investigation-pd02-stale-forecast | machine | 0.777778 | — | no | yes | no | no |
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
| gt-04-investigation-turn-budget-partial | machine | 1.000000 | — | no | yes | no | no |
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
| gt-02-investigation-pd02-stale-forecast | machine | 0.777778 | — | no | yes | no | no |
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
| gt-09-enrichment-value-and-close-date-proposal | 5 | 0.800000 | 0.960000 | 1.000000 | 0.800000 |
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
| timestamp | 2026-10-29T04:02:16.854726+00:00 |
| model_id | openai.gpt-6.1-sol |
| effort | medium |
| dataset_seed | 20260101 |
| runtime_identifier | live-aws-lambda-dispatch |
| price_table_version | 1.2.0 |
| answer_style_sha256 | 953efa6d0d26da1fb09cf2cdf625c78f0bd8f7d04da7652e581e623edea67f47 |
| prompt_bundle_sha256 | 77723af1ff2c9b216e100c7b352e9729dc4063fe2edd0bd7f3c12754fdb1ce1d |

### Usage and Cost

| field | value |
| --- | --- |
| model_id | openai.gpt-6.1-sol |
| included_invocation_count | 115 |
| input_tokens | 692274 |
| output_tokens | 23443 |
| cache_write_tokens | 140222 |
| cache_read_tokens | 551568 |
| cache_min_checkpoint_tokens | 1024 |
| latency_measured | yes |
| latency_percentile_method | nearest-rank |
| task_latency_ms_p50 | 7237.658750 |
| task_latency_ms_p95 | 35419.177416 |
| cost_withheld_provisional | no |
| cost_input | 0.000968 |
| cost_output | 0.234430 |
| cost_cache_write | 0.350555 |
| cost_cache_read | 0.055157 |
| cost_excluding_cache | 0.235398 |
| cost_cache_total | 0.405712 |
| cost_total | 0.641110 |
| price_table_version | 1.2.0 |

Usage and cost for this arm are a lower bound: in 5 task run(s) (gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial, gt-04-investigation-turn-budget-partial) a model call ended before reporting token usage, so its tokens and cost are not counted in the figures above.

Call latency by tier (ms):

| tier | p50 | p95 | count |
| --- | --- | --- | --- |
| supervisor | 2108.646875 | 5129.901250 | 242 |
| subagent | 1022.857542 | 2781.642875 | 193 |
| tool | 696.077333 | 1163.845291 | 336 |

Per tier / role / model:

| tier | role | model_id | included_invocation_count | input_tokens | output_tokens | cache_read_tokens | cache_write_tokens | call_latency_measured | call_latency_ms_p50 | call_latency_ms_p95 | cost_withheld_provisional | cost_input | cost_output | cost_cache_read | cost_cache_write | cost_excluding_cache | cost_cache_total | cost_total | price_table_version |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| subagent | analytics | openai.gpt-6.1-terra | 10 | 20544 | 1225 | 0 | 15656 | yes | 1022.857542 | 1797.096875 | no | 0.004888 | 0.006125 | 0.000000 | 0.019570 | 0.011013 | 0.019570 | 0.030583 | 1.2.0 |
| subagent | mutation | openai.gpt-6.1-terra | 132 | 489031 | 10935 | 417522 | 71245 | yes | 984.310083 | 1652.462500 | no | 0.000264 | 0.054675 | 0.020876 | 0.089056 | 0.054939 | 0.109932 | 0.164871 | 1.2.0 |
| subagent | research | openai.gpt-6.1-terra | 51 | 1250629 | 16350 | 821589 | 421947 | yes | 1263.867083 | 6111.557083 | no | 0.007093 | 0.081750 | 0.041079 | 0.527434 | 0.088843 | 0.568513 | 0.657356 | 1.2.0 |
| supervisor | supervisor | openai.gpt-6.1-sol | 242 | 692274 | 23443 | 551568 | 140222 | yes | 2108.646875 | 5129.901250 | no | 0.000968 | 0.234430 | 0.055157 | 0.350555 | 0.235398 | 0.405712 | 0.641110 | 1.2.0 |
| tool | analytics | openai.gpt-6-luna | 10 | 2980 | 480 | 0 | 0 | yes | 638.964667 | 824.001792 | no | 0.000298 | 0.000240 | 0.000000 | 0.000000 | 0.000538 | 0.000000 | 0.000538 | 1.2.0 |
| tool | mutation | openai.gpt-6-luna | 79 | 25004 | 7468 | 0 | 0 | yes | 913.225541 | 1470.757250 | no | 0.002500 | 0.003734 | 0.000000 | 0.000000 | 0.006234 | 0.000000 | 0.006234 | 1.2.0 |
| tool | research | openai.gpt-6-luna | 247 | 40125 | 11900 | 0 | 0 | yes | 670.447292 | 941.605208 | no | 0.004012 | 0.005950 | 0.000000 | 0.000000 | 0.009963 | 0.000000 | 0.009963 | 1.2.0 |

Per-tier summary:

| tier | input_tokens | output_tokens | cost | latency_p50_ms | latency_p95_ms | success (met/applicable) | success_rate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| supervisor | 692274 | 23443 | 0.641110 | 2108.646875 | 5129.901250 | 120/120 | 1.000000 |
| subagent | 1760204 | 28510 | 0.852811 | 1022.857542 | 2781.642875 | 115/115 | 1.000000 |
| tool | 68109 | 19848 | 0.016735 | 696.077333 | 1163.845291 | 95/95 | 1.000000 |

Per tool:

| tool_name | role | count | call_latency_ms_p50 | call_latency_ms_p95 | truncated_count | outcome_data | outcome_no_match | outcome_denied | outcome_tool_failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| create_note | mutation | 10 | 369.718916 | 1233.895667 | 0 | 10 | 0 | 0 | 0 |
| get_account | mutation | 11 | 269.546416 | 339.247000 | 0 | 11 | 0 | 0 | 0 |
| get_account | research | 10 | 268.442625 | 396.207792 | 0 | 10 | 0 | 0 | 0 |
| get_account | supervisor | 5 | 282.450916 | 322.254833 | 0 | 5 | 0 | 0 | 0 |
| get_contact | mutation | 1 | 223.148959 | 223.148959 | 0 | 1 | 0 | 0 | 0 |
| get_contact | research | 29 | 242.467583 | 296.410042 | 0 | 29 | 0 | 0 | 0 |
| get_contact | supervisor | 5 | 245.258292 | 273.146083 | 0 | 5 | 0 | 0 | 0 |
| get_opportunity | mutation | 6 | 312.354916 | 341.645416 | 0 | 6 | 0 | 0 | 0 |
| get_opportunity | research | 197 | 296.631708 | 403.424458 | 0 | 197 | 0 | 0 | 0 |
| get_opportunity | supervisor | 25 | 289.559583 | 354.792917 | 0 | 15 | 5 | 5 | 0 |
| list_activities | mutation | 1 | 290.664542 | 290.664542 | 0 | 1 | 0 | 0 | 0 |
| list_activities | research | 5 | 313.815458 | 400.238208 | 0 | 5 | 0 | 0 | 0 |
| list_activities | supervisor | 10 | 0.467334 | 343.737958 | 0 | 5 | 0 | 0 | 5 |
| list_audit_records | research | 1 | 1839.384667 | 1839.384667 | 0 | 1 | 0 | 0 | 0 |
| list_proposals | supervisor | 5 | 304.312333 | 415.105875 | 0 | 5 | 0 | 0 | 0 |
| log_activity | mutation | 10 | 380.720084 | 2017.808000 | 0 | 10 | 0 | 0 | 0 |
| pipeline_aggregate | supervisor | 5 | 1055.497542 | 1315.181125 | 0 | 5 | 0 | 0 | 0 |
| search_accounts | mutation | 5 | 310.584750 | 1262.437083 | 0 | 5 | 0 | 0 | 0 |
| search_opportunities | analytics | 10 | 127.048375 | 301.478584 | 0 | 5 | 0 | 5 | 0 |
| search_opportunities | research | 5 | 278.759959 | 291.046292 | 0 | 5 | 0 | 0 | 0 |
| search_opportunities | supervisor | 5 | 112.732250 | 114.818459 | 0 | 0 | 0 | 5 | 0 |
| submit_proposal | mutation | 25 | 317.173042 | 387.050833 | 0 | 25 | 0 | 0 | 0 |
| update_account | mutation | 5 | 442.582583 | 594.826500 | 0 | 5 | 0 | 0 | 0 |
| update_contact | mutation | 5 | 367.301833 | 2018.741333 | 0 | 5 | 0 | 0 | 0 |

Excluded invocations: 0 total (cold_start: 0, retry: 0, throttle: 0)

### Aggregate Score

| field | value |
| --- | --- |
| scored_task_count | 120 |
| unscored_task_count | 0 |
| mean_correctness | 1.000000 |
| bad_write_rate | 0.000000 |
| tool_call_correct_rate | 1.000000 |

`bad_write_rate` basis: all 250 declared `must_not_change` assertion(s) in this arm had a captured pre-turn baseline, so the figure is a measurement over them (src/eval/matrix.py).

`mean_correctness` composition: negative guards a do-nothing turn satisfies are 0.8154 of the assertion mass this figure averages — the mean over the 120 scored tasks of each task's own guard share, weighted exactly as `mean_correctness` itself is, rather than the ratio pooled over assertions (pooled totals, for context and not the basis of the figure just quoted: 675 guard assertions and 145 positive ones across those tasks). So the figure is not a task-achievement rate. Taken alone, 145 of 145 positive assertions were satisfied (1.0000); the guards alone were satisfied at 1.0000 (src/eval/matrix.py).

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
| gt-21-enrichment-pd04-multi-field-proposal | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-22-investigation-pd05-report-only | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-23-investigation-pd06-ambiguous-contact | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
| gt-24-enrichment-pd07-multi-step-write | 5 | 1.000000 | 1.000000 | 1.000000 | 1.000000 |
