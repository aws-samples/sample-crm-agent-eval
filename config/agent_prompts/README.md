# Agent prompts

One YAML file per agent system prompt, loaded by `src/agents/agent_prompts.py`
(`load_agent_prompts()`), which returns every prompt keyed by file stem in sorted file order. Each
text is sent identically to every model family.

| File | Sent as |
|---|---|
| [`supervisor.yaml`](supervisor.yaml) | the first system message of every chat and eval turn, ahead of the answer style ([`../answer_style.yaml`](../answer_style.yaml)) |
| [`subagent.yaml`](subagent.yaml) | each delegation's system message, before the intent; `{role}` and `{purpose}` are filled from the subagent's `agents.subagents.base.SubagentSpec` |

Each file has exactly two keys:

```yaml
schema_version: 1
prompt: |
  The prompt text.
```

Any other shape (a missing or extra key, another `schema_version`, an empty `prompt`) is rejected
with a `ValueError`.

Every prompt here is part of the prompt bundle (`src/agents/prompt_bundle.py`). Its SHA-256 is
recorded in eval run provenance as `prompt_bundle_sha256`, so editing, adding or removing a file
changes the digest of every later sweep. [`docs/eval-methodology.md`](../../docs/eval-methodology.md)
describes what else the bundle holds.
