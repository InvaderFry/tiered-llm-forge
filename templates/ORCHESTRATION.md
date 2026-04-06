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
# GOOGLE_API_KEY=your-google-ai-studio-key-here   # optional
```

Get keys from:
- **Groq:** https://console.groq.com → API Keys
- **Google AI Studio (optional):** https://aistudio.google.com → Get API Key

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

---

## Phase 4: Review Specs (Manual Checkpoint)

Read `specs/SpecsReadMe.md` and check:
- Each task touches only one file
- Tests cover acceptance criteria
- Dependencies are ordered correctly
- Specs include enough context about existing patterns

Edit spec files directly if anything looks wrong. This is your last cheap
chance to catch mistakes.

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

1. Creates a `task/task-NNN-name` git branch
2. Tries all primary tier models with fallback (3 attempts)
3. Escalates to the escalation tier if primary fails (2 attempts)
4. Writes `specs/FAILED-task-NNN-name.log` if all attempts fail
5. Returns to default branch, moves to next task

**Re-running is safe.** Passing branches are skipped. Failing branches go
straight to Claude review.

### Model routing

Models are configured in `models.yaml`. Default tiers:

| Tier | Models | When used |
|------|--------|-----------|
| Primary | Qwen3 32B → Kimi K2 → Llama 4 Scout | Normal tasks, fallback on rate limit |
| Escalation | GPT-OSS 120B | After primary tier exhausted |
| Large context | Llama 4 Scout | Specs > 16K chars |

---

## Phase 6: Fix Failures with Claude

```bash
ls specs/FAILED-*.log    # check for failures
claude
```

```
Fix the failures.
```

Claude reads the failure logs, diagnoses root cause, fixes on the task branch,
and confirms tests pass. Then re-run:

```bash
make run
```

---

## Phase 7: Review and Merge

After all tasks pass:

```
Review the task branches and merge them.
```

Claude diffs each branch against its spec and decides:
- **MERGE** — squash-merge automatically
- **FIX THEN MERGE** — correct quality issue, then merge
- **FLAG** — writes REVIEW notes, does not merge

---

## Daily Workflow Summary

```
1. cd ~/projects/{{PROJECT_NAME}}
2. export $(cat .env | grep -v '#' | xargs)
3. claude                                      # plan
   > "Build [feature]. Generate specs and tests."
4. make validate                               # check specs
5. Review specs/ manually                      # catch anything obvious
6. make run                                    # run pipeline
7. claude                                      # fix failures (if any)
   > "Fix the failures."
8. make run                                    # re-run until all pass
9. claude                                      # review and merge
   > "Review the task branches and merge them."
```

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
| Check failures | `ls specs/FAILED-*.log` |
| See task branches | `git branch --list "task/*"` |
| Pipeline state | `cat pipeline-state.json` |
| See all commands | `make help` |

---

## Troubleshooting

**Aider can't find Groq models**
→ Run `export $(cat .env | grep -v '#' | xargs)` first.

**pytest can't import src modules**
→ Make sure `src/__init__.py` exists and you're running pytest from the project root.

**Rate limit 429 from Groq**
→ The orchestrator automatically falls back through the model chain (configured
in `models.yaml`). If all models are exhausted, wait a few minutes — Groq
limits reset hourly.

**Task branch already exists**
→ The orchestrator handles this automatically: skips passing branches, escalates
failing branches to Claude review.

**Spec validation errors**
→ Run `make validate` and fix all errors before `make run`.

**Pipeline crashed mid-run**
→ Just re-run `make run`. The pipeline state and git branches persist. Passing
tasks are skipped, failed tasks are detected.

**Want to add a new model provider**
→ Edit `models.yaml` and add models to the appropriate tier. The orchestrator
reads model names directly — any model Aider supports will work.
