# April Model Implementation Plan

Status: implementation plan only. This file defines what to change later. It does not change repo behavior by itself.

## Objective

Update the repo's default model-routing setup to match the April 14, 2026 research in `free-tier-coding-models-comparison-20260414.md` while preserving the current workflow:

- Claude remains the planner.
- Stable models are the default.
- Preview models remain optional and are not part of the default path.

## Primary Outcome

The default generated project should use this routing shape:

1. Claude plans and writes specs.
2. `primary` runs a stable Groq implementation model.
3. `escalation` runs stable Groq quality-first fallback models.
4. `gemini` is a stable cross-provider overflow tier.
5. No post-pass review stage is added in the first change.

Phase 1 routing summary:

`Claude plan -> GPT-OSS 20B primary -> GPT-OSS 120B / Llama 3.3 70B escalation -> Gemini 2.5 Flash overflow`

## Current Repo Constraints

These constraints must drive the implementation:

- The orchestrator is tier-based, not role-based.
- Retries happen at the tier level.
- `primary`, `escalation`, and `gemini` are the current tier names.
- The integration repair path currently uses the `gemini` tier.
- There is no post-pass review stage today.
- The config loader currently preserves only `id` and `max_input_tokens` from each model entry.
- The router treats `max_input_tokens` as a hard prescreen skip threshold.

Implication:

- Phase 1 should be limited to config, validation, docs, and tests.
- Review-stage behavior is a later code change, not part of the first implementation.

## Phase 1: Exact Scope

Phase 1 is the immediate implementation target.

### In Scope

- Update default model ordering and retry counts in `templates/models.yaml`.
- Remove dead defaults from config and docs.
- Expand preflight validation to reject known dead model IDs.
- Update docs so they describe the new default routing accurately.
- Update tests and fixtures that assume old model IDs or old fallback order.

### Out of Scope

- No new post-pass review stage.
- No new tier names.
- No quota-aware routing logic.
- No metadata schema expansion beyond what the current loader supports.
- No generator UX for choosing profiles.
- No hidden routing logic that changes behavior without being represented in config.

### Files Expected To Change In Phase 1

- `templates/models.yaml`
- `templates/orchestrator/preflight.py`
- `README.md`
- `templates/ORCHESTRATION.md`
- `templates/CLAUDE.md`
- `tests/test_model_router.py`
- `tests/test_preflight.py`

Also search for stale references in:

- `tests/test_spec_parser.py`
- `templates/CLAUDE.md`
- any other docs or examples that still mention old defaults

## Phase 1: Intended Default Routing

### Routing Intent

- `primary` should be a single stable implementation model with multiple retries.
- `escalation` should contain stable Groq fallback models with a small retry count.
- `gemini` should be a stable overflow tier, not the main worker.

### Preferred Model Choices

Use these model choices after verifying the exact provider IDs at implementation time:

- `primary`
  - `GPT-OSS 20B`
- `escalation`
  - `GPT-OSS 120B`
  - `Llama 3.3 70B`
- `gemini`
  - `Gemini 2.5 Flash`
- `weak_model`
  - keep `groq/llama-3.1-8b-instant` unless a separate reason emerges to change it

### Retry Intent

- `primary.retries = 3`
- `escalation.retries = 1`
- `gemini.retries = 1`

Reason:

- Multiple models inside a tier are retried as part of each tier attempt.
- Because of that, `primary` should stay single-model if it has multiple retries.
- `escalation` can contain multiple fallback models because it has only one retry.

## Phase 1: Config Shape

This is the target structure to implement after verifying exact model IDs:

```yaml
tiers:
  - name: primary
    models:
      - id: <verified Groq GPT-OSS 20B model id>
        max_input_tokens: <validated prescreen cap>
    retries: 3
    aider_timeout_seconds: 180

  - name: escalation
    models:
      - id: <verified Groq GPT-OSS 120B model id>
        max_input_tokens: <validated prescreen cap>
      - id: <verified Groq Llama 3.3 70B model id>
        max_input_tokens: <validated prescreen cap>
    retries: 1
    aider_timeout_seconds: 300

  - name: gemini
    models:
      - id: <verified Gemini 2.5 Flash model id>
    retries: 1
    aider_timeout_seconds: 600

weak_model: groq/llama-3.1-8b-instant
```

Implementation rules:

- Do not substitute preview models into the default path.
- Do not assume the exact provider IDs in planning notes are correct without verification.
- Do not keep `Kimi K2` anywhere in the default config.
- Do not keep `Llama 4 Scout` in the default config.

## `max_input_tokens` Rules

This area is easy to implement incorrectly.

### What The Field Means In This Repo

In this repo, `max_input_tokens` is not treated as the provider context window. It is used as a conservative prescreen guardrail by the router.

### Implementation Guidance

- Start with cautious values only after checking current request sizes.
- Validate the chosen caps against the current request estimator and context budget.
- Expect to tune these values after dry-run and test feedback.

### Do Not Do This

- Do not copy provider context-window numbers directly into `max_input_tokens`.
- Do not set values so low that normal tasks get skipped in `primary` or `escalation`.
- Do not change config comments in a way that implies these are literal context windows.

## Gemini Tier Clarification

The `gemini` tier is not only task overflow in the current repo.

It is used by:

- task-level fallback after `escalation`
- integration-level automated repair

Implication:

- `Gemini 2.5 Flash` is a shared emergency budget.
- Do not describe it in docs as if it is only used during ordinary task retries.

## Preflight Requirements

Phase 1 should expand the denylist in `templates/orchestrator/preflight.py`.

Minimum requirement:

- reject `groq/moonshotai/kimi-k2-instruct`
- keep rejecting known bad Gemini placeholders
- reject any clearly unavailable or placeholder Gemini Pro entries if they appear in config or config fixtures used by tests

Implementation rule:

- Preflight should fail before runtime when the config contains a model ID that the project has already decided is dead or unsupported.
- Preflight should validate config inputs, not scan prose documentation files.

## Documentation Requirements

After Phase 1, repo docs and generated-project docs must agree on the same default routing.

### Must Be Updated

- Repo doc:
  - `README.md`
- Generated-project docs:
  - `templates/ORCHESTRATION.md`
  - `templates/CLAUDE.md`

### What The Docs Must Say

- default flow is stable-first
- Claude remains the planner
- `primary` is `GPT-OSS 20B`
- `escalation` is stable Groq fallback
- `gemini` is overflow and integration-repair backup
- preview models are optional and not default

### What The Docs Must Not Say

- `Kimi K2` is a default model
- `Llama 4 Scout` is the default escalation model
- Gemini preview models are the default daily-driver path

Implementation rule:

- `templates/CLAUDE.md` must not keep the old top-line model chain once Phase 1 changes are made.
- `templates/CLAUDE.md` should also be checked for other stale workflow text while it is open for edits. In particular, do not leave comments that imply spec context is still stripped to save tokens if that is no longer true in the current orchestrator.
- Do not update `templates/models.yaml` without updating the docs that describe the model order.

## Test Update Requirements

Phase 1 must update any tests that encode old defaults.

Likely starting points:

- `tests/test_model_router.py`
- `tests/test_preflight.py`

Also search for:

- old fixture configs using `Kimi K2`
- old fixture configs using `Llama 4 Scout`
- tests that assume old fallback order
- tests or fixtures that still describe old defaults in generated-project documentation expectations
- `tests/test_spec_parser.py` fixtures that may look stale but may actually be generic test scaffolding rather than default-routing assertions

Implementation rule:

- Test fixtures should reflect the new stable defaults where they are meant to represent real defaults.
- Tests that are intentionally generic should stay generic.
- Do not rewrite `tests/test_spec_parser.py` just to match the new defaults unless the fixture is actually asserting default-routing behavior.

## Acceptance Criteria For Phase 1

Phase 1 is complete only when all of the following are true:

1. `templates/models.yaml` uses the new stable-first routing.
2. Default config no longer references removed models such as `Kimi K2`.
3. Preflight rejects known dead model IDs before runtime.
4. Docs describe the same routing that the config implements.
5. Unit tests no longer assert the old routing or old dead model IDs as defaults.
6. No post-pass review stage has been added yet.
7. No preview model has been introduced into the default path.
8. `templates/CLAUDE.md` no longer describes the old model chain.

## Phase 2: Optional Profile Support

This is a later change.

### Goal

Support an opt-in preview-overflow configuration without changing the stable default.

### Implementation Shape

- Keep `models.yaml` as the stable generated default.
- Add a separate config file such as `models.preview-overflow.yaml`.
- Use the existing `python3 -m orchestrator --config <path>` path first.
- Only add generator UX for profile selection if it is still needed after the config-file approach works.

### Preview Models Intended For That Profile

- `Gemini 3.1 Flash Lite`
- `Qwen3 32B`

Implementation rule:

- Do not merge preview-overflow behavior into the stable default config.

## Phase 3: Review-Stage Behavior

This is a later architecture change.

### Goal

Add a conditional review/fix stage after a task passes when the task meets review triggers.

### Triggers To Consider

- task passed only after retries
- large diff
- risky file or interface changes
- integration failed once after task-level success

### Intended Review Path

- first choice: `GPT-OSS 120B`
- overflow choice: `Gemini 2.5 Flash`
- optional preview-overflow choice: `Gemini 3.1 Flash Lite`

Implementation rule:

- Review must be conditional, not universal.
- Do not implement this as part of Phase 1.

## Explicit Do/Do Not Summary

### Do

- make the default routing stable-first
- keep Claude as planner
- keep `primary` single-model with multiple retries
- keep `escalation` as the place for stronger fallback models
- keep Gemini as stable overflow in the default path
- update docs, validation, and tests together

### Do Not

- do not add preview models to the default path
- do not add a review stage in the same change as the default config update
- do not invent new config fields in Phase 1
- do not rely on unverified provider model IDs
- do not leave dead model references in docs or fixtures

## Execution Order

Implement in this order:

1. Verify exact provider model IDs to use.
2. Update `templates/models.yaml`.
3. Expand preflight denylist.
4. Update docs, including `templates/CLAUDE.md`.
5. Update tests and fixtures.
6. Run validation and test checks.

If exact provider IDs cannot be verified confidently at implementation time:

- stop before changing the default config
- do not guess model IDs
- leave placeholders only in planning documents, not in executable config

## Final Decision

Implement this first:

`Claude plans, GPT-OSS 20B is the default implementation model, GPT-OSS 120B and Llama 3.3 70B are stable Groq escalation, Gemini 2.5 Flash is stable overflow, and preview models remain optional.`
