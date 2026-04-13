# {{PROJECT_NAME}}

Tiered LLM coding workflow project.
Claude plans and reviews. Free Groq models implement.

---

## Phase 1: Setup (once)

### Activate the virtual environment

The bootstrap script already created `.venv` and installed dependencies.
Just activate it:

```bash
source .venv/bin/activate
```

To reinstall from scratch: `make setup`

### Set your API keys

Edit `.env` in the project root:

```bash
nano .env
```

```env
GROQ_API_KEY=your-groq-key-here
GOOGLE_API_KEY=your-google-ai-studio-key-here
# GEMINI_API_KEY=your-google-ai-studio-key-here   # legacy alias; GOOGLE_API_KEY is preferred
```

Get keys from:
- **Groq:** https://console.groq.com → API Keys
- **Google AI Studio (Gemini):** https://aistudio.google.com → Get API Key

> `.env` is in `.gitignore` — it will never be committed.

Load keys into the shell:

```bash
export $(cat .env | grep -v '#' | xargs)
```

Add that line to `~/.bashrc` for automatic loading.

---

## Phase 2: Plan with Claude Code

```bash
claude
```

CLAUDE.md is loaded automatically. Describe what you want to build:

```
I want to build [feature]. Generate the specs and tests.
```

Claude writes spec files with YAML frontmatter, test files, and a
`specs/SpecsReadMe.md` summary.

**Other useful prompts:**

```
Review the specs you just created.
Add a task for [new requirement].
```

---

## Phase 3: Validate Specs

Before running the pipeline, validate all specs:

```bash
make validate
```

This checks:
- Required frontmatter fields (target, test, dependencies)
- Test files exist
- No dependency cycles
- Token budget compliance
- Prints the dependency-ordered execution plan

Fix any errors before proceeding.

Validate config and provider env before spending model tokens:

```bash
make preflight
```

This catches:
- Missing provider API keys
- Invalid or placeholder model IDs in `models.yaml`
- Runtime/config mismatches before the task loop starts

---

## Phase 4: Review Specs (Manual Checkpoint)

Read `specs/SpecsReadMe.md` and check:
- Each task touches only one file
- Tests cover acceptance criteria
- Dependencies are ordered correctly
- Specs include enough context about existing patterns

Edit spec files directly if anything looks wrong. This is your last cheap
chance to catch mistakes.

### Commit the Task Inputs

Before the orchestrator runs, the task spec files and their referenced test
files must already be tracked by git and have no uncommitted changes. This is
required for both sequential integration merges and parallel worktree runs.

Commit them now:

```bash
git add specs tests
git commit -m "chore: add task specs and tests"
```

---

## Phase 5: Run the Pipeline

Preview what will happen:

```bash
make dry-run
```

Run for real:

```bash
make run
```

The orchestrator processes each spec in dependency order:

1. Creates a `task/task-NNN-name` git branch **from its dependency branch(es)**,
   not from the default branch. A task with one dependency is stacked on
   that dependency; a task with several dependencies starts from the
   default branch and merges each dep branch in before running Aider.
2. Attaches the spec file, test file, and dependency target files
   as read-only context for Aider, but trims that context to a
   configurable file/size budget so large fan-in tasks do not overflow
   provider TPM caps.
3. Tries all primary tier models with fallback (3 attempts).
4. Escalates to the escalation tier if primary fails (2 attempts).
5. If both tiers exhaust, tries the **Gemini tier** (1 attempt per model).
   Skips gracefully when the daily API quota is exhausted.
6. Writes `forgeLogs/FAILED-task-NNN-name-<timestamp>.log` (tagged with a
   failure class) if all automated tiers fail.
6. Records per-task attempts, attempted models, base SHA, and both terminal
   and test failure classes in `pipeline-state.json`.
7. Returns to default branch, moves to next task.

After every task passes, the orchestrator runs an **integration gate**
(see Phase 7 below).

**Re-running is safe.** Passing branches are skipped. Failing branches go
straight to Claude review.

### Parallel mode

Run tasks concurrently within each dependency wave:

```bash
make parallel          # default: 4 workers
python3 -m orchestrator --parallel 8   # override worker count
```

The orchestrator partitions the dependency graph into waves — groups of
tasks with no mutual dependencies — and runs each wave's tasks
simultaneously in isolated git worktrees via a thread pool.

**Pros:**

- **Faster wall-clock time.** Independent tasks run concurrently instead of
  waiting in a serial queue. A pipeline with 3 independent tasks followed
  by 1 dependent task finishes in roughly 2 rounds instead of 4.
- **Better rate-limit utilisation.** The per-model rate-limit coordinator is
  shared across threads, so while one task sleeps through a 429, another
  task can hit a different model.
- **No manual configuration.** The wave grouping is derived automatically
  from the dependency graph in each spec's frontmatter.

**Cons:**

- **Disk usage.** Each concurrent task gets its own git worktree (a full
  working-tree copy). For large repos this can be significant, though
  worktrees share the object store.
- **Interleaved log output.** Multiple tasks write to the console and
  `forgeLogs/orchestrator-<timestamp>.log` simultaneously. Use `--verbose`
  and grep by task name when debugging.
- **Rate-limit amplification.** More concurrent requests can hit provider
  rate limits faster. The coordinator mitigates this, and fixed cooldowns are
  only applied after real work or pending retry windows, but wide waves can
  still create more provider pressure than sequential runs.
- **Not the default.** Sequential mode (`make run`) is simpler and easier to
  debug. Prefer it for first runs or when diagnosing failures.

Single-task waves skip worktree overhead and run directly, so `--parallel`
is never slower than sequential for fully linear dependency chains.

### Model routing

Models are configured in `models.yaml`. Default tiers, tried in order:

| Tier | Models | Trigger | API key |
|------|--------|---------|---------|
| Primary | Qwen3 32B → Kimi K2 → Llama 4 Scout | Every task, 3 attempts | `GROQ_API_KEY` |
| Escalation | GPT-OSS 120B | Primary exhausted, 2 attempts | `GROQ_API_KEY` |
| Gemini | Gemini 2.5 Flash | Escalation exhausted, 1 attempt each | `GOOGLE_API_KEY` |

The Gemini tier uses **daily quota** semantics: if a model's free-tier quota is
exhausted for the day, it is skipped immediately (no sleep) and the next model
is tried. When both Gemini models are exhausted, the task is flagged for Claude
review and a note is printed at the end of the run.

---

## Phase 6: Fix Failures with Claude

The pipeline automatically tries every automated tier (primary → escalation →
Gemini) before flagging a task for human review. If you see failures, they have
already exhausted all automated options.

```bash
ls forgeLogs/FAILED-*.log    # check for failures
claude
```

```
Fix the failures.
```

If the summary noted `Gemini daily quota was exhausted`, the Gemini tier was
attempted but had no quota remaining. Retry tomorrow for a free automated
retry, or fix with Claude now — both paths work.

Claude reads the failure logs, diagnoses root cause, fixes on the task branch,
and confirms tests pass. Then re-run:

```bash
make run
```

---

## Phase 7: Integration Gate (automatic)

When the per-task loop finishes with zero failures, the orchestrator
automatically assembles an integration branch and runs the full test
suite against it:

1. Creates `integration/run-<timestamp>` from the default branch.
2. Merges each passing `task/*` branch with `--no-ff` in dependency
   order.
3. Runs `pytest tests/` on the combined result.

Outcomes:

- **Clean:** the integration branch is left in place and ready for
  human merge review in Phase 8. `pipeline-state.json` records
  `integration.status = "passed"`.
- **Merge conflict:** writes `forgeLogs/INTEGRATION-FAILED-<timestamp>.log`
  with the offending task branch and deletes the integration branch. Resolve
  by reworking the spec's dependency graph or the conflicting task,
  then re-run `make run`.
- **Tests failed on the combined branch:** the Gemini tier automatically
  attempts to fix the failure. If Gemini fixes it, the integration gate
  proceeds as successful. If Gemini cannot fix it (or quota is exhausted),
  writes `forgeLogs/INTEGRATION-FAILED-<timestamp>.log` with the full pytest
  output and **keeps** the integration branch so you can reproduce the failure
  with `git checkout integration/run-<timestamp>` and iterate.

The integration gate is skipped if any task in the current run failed.
Fix the per-task failures first, then re-run — the passing tasks are
skipped automatically and the gate runs when the last one clears.

---

## Phase 8: Review and Merge

After the integration gate passes:

```
Review the task branches and merge them.
```

Claude diffs each branch against its spec and decides:
- **MERGE** — squash-merge automatically
- **FIX THEN MERGE** — correct quality issue, then merge
- **FLAG** — writes REVIEW notes, does not merge

See `docs/MERGE_CHECKLIST.md` for the full procedure. The checklist
resolves the default branch dynamically instead of assuming `main`.

---

## Daily Workflow Summary

```
1. cd ~/projects/{{PROJECT_NAME}}
2. export $(cat .env | grep -v '#' | xargs)
3. claude                                      # plan
   > "Build [feature]. Generate specs and tests."
4. make validate                               # check specs
5. Review specs/ manually                      # catch anything obvious
6. make run                                    # run pipeline + integration gate
7. claude                                      # fix failures (if any)
   > "Fix the failures."
8. make run                                    # re-run until all pass + gate is clean
9. claude                                      # review and merge
   > "Review the task branches and merge them."
```

Steps 6 and 8 run the integration gate automatically once every task
passes. A clean gate leaves an `integration/run-*` branch behind for
Phase 8; a failed gate writes `forgeLogs/INTEGRATION-FAILED-<timestamp>.log`.

---

## Cheat Sheet

| What | Command |
|------|---------|
| Activate venv | `source .venv/bin/activate` |
| Reinstall deps | `make setup` |
| Load env vars | `export $(cat .env \| grep -v '#' \| xargs)` |
| Open Claude Code | `claude` |
| Validate specs | `make validate` |
| Preview pipeline | `make dry-run` |
| Run pipeline | `make run` |
| Resume crashed run | `make resume` |
| Run tasks concurrently in waves | `make parallel` |
| Check failures | `ls forgeLogs/FAILED-*.log` |
| Check integration gate | `ls forgeLogs/INTEGRATION-FAILED-*.log 2>/dev/null; git branch --list "integration/*"` |
| See task branches | `git branch --list "task/*"` |
| Pipeline state | `cat pipeline-state.json` |
| Debug log (latest run) | `ls -t forgeLogs/orchestrator-*.log \| head -1 \| xargs cat` |
| See all commands | `make help` |

---

## Troubleshooting

**Aider can't find Groq models**
→ Run `export $(cat .env | grep -v '#' | xargs)` first.

**Gemini tier skipped / not attempted**
→ Check that `GOOGLE_API_KEY` is set in `.env` and exported. The legacy
`GEMINI_API_KEY` alias is also accepted and mirrored automatically, but
`GOOGLE_API_KEY` is the canonical setting. If the key is missing, Aider
returns an auth error and the tier is marked as failed (not quota-exhausted),
which still triggers Claude review.

**"Gemini daily quota was exhausted" in the summary**
→ The free Gemini API tier has a daily request cap that resets at midnight UTC.
The orchestrator already skipped the exhausted models cleanly. Options:
- **Wait and retry:** re-run tomorrow once the quota resets.
- **Fix with Claude now:** the failure logs contain everything Claude needs.
- **Upgrade:** use a paid Gemini API plan to remove the daily cap.

**pytest can't import src modules**
→ Make sure `src/__init__.py` exists and you're running pytest from the project root.

**Rate limit 429 from Groq**
→ The orchestrator handles this at two levels. First, a per-model coordinator
records the "try again in Ns" hint from each 429 and sleeps exactly that
long before the next request to that model — across tasks, not just within
one retry loop. Second, `cooldown_seconds` in `models.yaml` provides a
defensive baseline between tasks for signals the parser misses, but it only
fires after real work or pending retry windows. If you are still seeing 429s
at the start of each task, raise `cooldown_seconds`. If
all models exhaust their retries, wait a few minutes — Groq limits reset
hourly.

**Task branch already exists**
→ The orchestrator handles this automatically: skips passing branches, escalates
failing branches to Claude review.

**Spec validation errors**
→ Run `make validate` and fix all errors before `make run`.

**Pipeline crashed mid-run**
→ Run `make resume`. This resumes each task from the exact attempt where it
left off (using the per-attempt log in `pipeline-state.json`) instead of
re-evaluating the whole branch. If you prefer a fresh start, `make run`
still works — passing branches are skipped, failing branches go to review.

**Integration gate failed with a merge conflict**
→ Read `forgeLogs/INTEGRATION-FAILED-<timestamp>.log` for the offending branch. Two tasks
are touching overlapping code — either split the overlap into a new spec
that both depend on, or add one of them as a dependency of the other so
they no longer run in parallel. Then `make run`.

**Integration gate failed the test suite**
→ The Gemini tier was already attempted automatically. If it also failed (or
quota was exhausted), the `integration/run-*` branch is kept on disk.
`git checkout` it, run `pytest tests/` to reproduce, fix the cross-task
regression on the offending task branch (not the integration branch), and
`make run`. The orchestrator will rebuild a fresh integration branch on the
next pass.

**I want to see per-model stats, costs, and failure classes**
→ `cat pipeline-state.json` — each task records `model`, `models_tried`,
`duration_seconds`, `failure_class`, `tokens_sent`, `tokens_received`,
and `cost_usd` (scraped from aider's stdout). `make run` also prints a
rolled-up summary at the end with totals and per-model token / dollar
counts so you can see whether the escalation tier is actually
earning its keep.

**Want to add a new model provider**
→ Edit `models.yaml` and add models to the appropriate tier. The orchestrator
reads model names directly — any model Aider supports will work.
