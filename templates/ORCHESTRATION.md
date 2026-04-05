# {{PROJECT_NAME}}

Tiered LLM coding workflow project.
Claude plans and reviews. Free Groq models implement.

---

## Phase 1: Set Your API Keys

Edit `.env` in the project root before running anything:

```bash
nano .env
```

Fill in your values:

```env
GROQ_API_KEY=your-groq-key-here
# GOOGLE_API_KEY=your-google-ai-studio-key-here   # optional
```

Get keys from:
- **Groq:** https://console.groq.com → API Keys
- **Google AI Studio (optional):** https://aistudio.google.com → Get API Key

> ⚠️ `.env` is in `.gitignore` — it will never be committed. Never paste keys
> into any other file that gets committed.

Load your keys into the shell before running the pipeline:

```bash
export $(cat .env | grep -v '#' | xargs)
```

Add that line to `~/.bashrc` to load keys automatically on every terminal open.

---

## Phase 2: Start LiteLLM (once per reboot)

LiteLLM is the routing proxy that handles Groq model fallback. Start it with Docker:

```bash
export $(cat .env | grep -v '#' | xargs)

docker run -d --name litellm -p 4000:4000 \
  -v $(pwd)/litellm-config.yaml:/app/config.yaml \
  -e GROQ_API_KEY=$GROQ_API_KEY \
  ghcr.io/berriai/litellm:main-latest \
  --config /app/config.yaml
```

After the first time, just run:

```bash
docker start litellm
```

Verify it's running:

```bash
curl http://localhost:4000/health
```

---

## Phase 3: Plan with Claude Code

Open Claude Code in this project directory:

```bash
claude
```

CLAUDE.md is already loaded — Claude Code knows the spec format, file locations,
naming conventions, and its role. You just need to describe what you want to build.

**Basic prompt (enough for most features):**

```
I want to build [feature]. Generate the specs and tests.
```

**Better prompt (give context, get better decomposition):**

```
I want to build [feature]. It should [key behaviours]. Generate the specs and tests.
```

Example:

```
I want to build a rate limiter for an API. It should track requests per user
per minute using a sliding window, reject over-limit requests with a 429
response, and persist state in Redis so it survives restarts. Generate the
specs and tests.
```

**Other useful prompts to keep handy:**

After specs are generated, before running the pipeline:
```
Review the specs you just created. Check for dependency ordering issues,
tasks that touch more than one file, and tests that might not be self-contained.
```

Adding to an existing codebase:
```
I want to add [feature] to the existing codebase. Read src/ first, then
generate specs that follow the same patterns already in use.
```

Fixing a failure:
```
Fix task-003. The log is at specs/FAILED-task-003-name.log.
```

Use `/status` inside Claude Code to check your remaining Pro usage budget.
Planning is your most valuable use of Claude tokens — describe the feature
well and let CLAUDE.md handle the process.

---

## Phase 4: Review Specs (Manual Checkpoint)

Before running the pipeline, read through the specs Claude generated:

- Each task touches only one file (or a minimal set)
- Tests actually cover the acceptance criteria
- Specs include enough context about existing code patterns
- Tasks are ordered correctly (dependencies first)

Edit spec files directly if anything looks wrong. This is your last cheap
chance to catch architectural mistakes before cheap models start generating code.

---

## Phase 5: Run the Pipeline

```bash
export $(cat .env | grep -v '#' | xargs)
python3 orchestrator.py
```

The orchestrator loops through every `specs/task-*.md` file and:

1. Creates a `task/task-NNN-name` git branch
2. Runs Aider with **Qwen3 32B → Kimi K2 → Llama 4 Scout** via LiteLLM (3 attempts)
3. Escalates to **GPT-OSS 120B** directly if primary tier fails (2 attempts)
4. Writes a `specs/FAILED-task-NNN-name.log` if all attempts fail
5. Returns to the default branch and moves to the next task

**Re-running is safe.** Tasks with passing branches are skipped automatically.
Tasks with failing branches go straight to Claude review without wasting retries.

### Model routing

| Condition | Model used |
|-----------|-----------|
| Normal task | Qwen3 32B via LiteLLM (falls back to Kimi K2 → Llama 4 Scout on 429) |
| Spec > ~4K tokens | Llama 4 Scout directly (30K TPM handles large context) |
| Primary tier fails all retries | GPT-OSS 120B directly (never via LiteLLM) |
| Commit messages | Llama 3.1 8B (14.4K RPD — never burns primary quota) |

---

## Phase 6: Review Failures with Claude

After the pipeline finishes, check for failures:

```bash
ls specs/FAILED-*.log
```

If there are failures, open Claude Code and use this prompt:

```
Fix the failures.
```

Claude will find every `specs/FAILED-*.log`, diagnose each one (checking
whether the spec or the implementation is at fault), fix the code on the task
branch, and confirm tests pass before committing. To fix a single task:

```
Fix task-003.
```

After Claude finishes, re-run the pipeline. Fixed branches are detected as
passing and skipped automatically.

---

## Phase 7: Review and Merge Passing Tasks

After the pipeline prints "All tasks passing", hand off to Claude for the
quality gate:

```
Review the task branches and merge them.
```

Claude will diff each branch against its spec and make one of three decisions:

- **MERGE** — squash-merges the branch into main automatically
- **FIX THEN MERGE** — corrects a quality issue on the branch, then merges
- **FLAG** — writes `specs/REVIEW-task-NNN-name.md` explaining why the branch
  doesn't meet the spec's intent; does not merge; you'll need to re-attempt

Claude summarises the outcome for every branch when done.

---

## Daily Workflow Summary

```
1. cd ~/projects/{{PROJECT_NAME}}
2. docker start litellm                         # if not already running
3. export $(cat .env | grep -v '#' | xargs)
4. claude                                       # open Claude Code
   > "I want to build [feature]. Generate the specs and tests."
   > "Review the specs you just created."      # optional sanity check
5. Review specs/ manually                       # Phase 4: catch anything obvious
6. python3 orchestrator.py                      # Phase 5: run pipeline
7. For failures: claude                         # Phase 6: fix failures
   > "Fix the failures."
8. python3 orchestrator.py                      # re-run until all pass
9. claude                                       # Phase 7: review and merge
   > "Review the task branches and merge them."
```

**Expected Claude Pro usage:** ~30–45 minutes of interactive Claude Code per day
(planning + failure review). The pipeline itself uses zero Claude tokens.

---

## Cheat Sheet

| What | Command |
|------|---------|
| Load env vars | `export $(cat .env \| grep -v '#' \| xargs)` |
| Start LiteLLM | `docker start litellm` |
| Open Claude Code | `claude` |
| Check Claude usage | `/status` inside Claude Code |
| Run pipeline | `python3 orchestrator.py` |
| Check failures | `ls specs/FAILED-*.log` |
| See task branches | `git branch --list "task/*"` |
| Diff a task branch | `git diff master..task/task-001-name` |

---

## Troubleshooting

**Aider can't find Groq models**
→ Run `export $(cat .env | grep -v '#' | xargs)` first. Add to `~/.bashrc` to make it permanent.

**LiteLLM container exits immediately**
→ Run `docker logs litellm`. Most common cause: missing `GROQ_API_KEY` env var or malformed `litellm-config.yaml`.

**pytest can't import src modules**
→ Make sure `src/__init__.py` exists and you're running pytest from the project root.

**Rate limit 429 from Groq**
→ LiteLLM falls back automatically to Kimi K2 → Llama 4 Scout. If all three are exhausted, wait a few minutes — Groq limits reset hourly.

**Task branch already exists error**
→ This shouldn't happen anymore. The orchestrator detects existing branches and either skips (if passing) or routes to Claude review (if failing).

**Claude Code hits usage limits**
→ Use `/status` to check budget before planning. Work during off-peak hours (before 5 AM PT or after 11 AM PT) to avoid tighter limits.
