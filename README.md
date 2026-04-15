# tiered-llm-forge

A project generator for the tiered LLM coding workflow. Run it once to scaffold
a new project, then work entirely inside that project.

---

## What this repo is

This is the **forge** — it lives on your machine and generates projects. You never
work inside this repo. Each generated project gets its own directory, git history,
and copy of all the workflow files.

---

## Prerequisites

- [Aider](https://aider.chat) installed and on your PATH
- A [Groq API key](https://console.groq.com)
- A [Google AI Studio / Gemini API key](https://aistudio.google.com) (free tier; used for Gemini overflow and integration repair)
- Python 3.10+ with `python3-venv` (`sudo apt install python3-venv` on Debian/Ubuntu)

---

## Quickstart

```bash
bash ~/projects/tiered-llm-forge/new-project.sh
```

This creates `~/projects/my-project-YYYYMMDD-HHMMSS/` with everything you need,
including a `.venv` with dependencies already installed. Then:

```bash
cd ~/projects/my-project-YYYYMMDD-HHMMSS/
source .venv/bin/activate
```

From here, follow the workflow in that project's `ORCHESTRATION.md`.

---

## Daily workflow (inside the generated project)

```
1.  Edit `.env` — add `GROQ_API_KEY` and `GOOGLE_API_KEY` (or legacy `GEMINI_API_KEY`) the first time only
2.  export $(cat .env | grep -v '#' | xargs)
3.  claude
    > "I want to build [feature]. Generate the specs and tests."
4.  make validate                    # check specs for errors
5.  make preflight                  # validate config + provider env
6.  Review specs/ manually
7.  git add specs tests && git commit -m "chore: add task specs and tests"
8.  make run                         # run the pipeline
9.  For failures: back to claude
    > "Fix the failures."
10. Repeat 8-9 until all tasks pass
11. Back to claude for the final quality gate
    > "Review the task branches and merge them."
```

The pipeline uses zero Claude tokens while it runs — it goes through stable
Groq implementation tiers first and uses Gemini via Aider as shared overflow
and integration-repair backup. Claude is only used for planning and for
failures that exhaust every automated stage.

---

## How the pipeline works

Each `specs/task-NNN-name.md` file is a task with YAML frontmatter that declares
the target file, test file, and dependencies. The orchestrator processes them in
dependency order:

1. Creates a `task/task-NNN-name` git branch **from its dependency branch(es)**
   — stacked on a single dep, or assembled via merge when there are several —
   so the task actually runs against its upstream code.
2. Attaches the spec file, test file, and dependency target files to
   Aider as read-only context, but trims that context to a configurable
   file/size budget so large fan-in tasks do not blow past provider TPM caps.
3. Tries the `primary` tier first: **GPT-OSS 20B** (3 attempts).
4. Escalates to stable Groq fallback models in `escalation`:
   **GPT-OSS 120B → Llama 3.3 70B** (1 tier attempt).
5. If both Groq tiers exhaust, tries the **Gemini tier**:
   **Gemini 2.5 Flash** as the shared overflow and integration-repair backup.
   If Gemini's daily API quota is exhausted, skips gracefully.
6. Writes `forgeLogs/FAILED-task-NNN-name-<timestamp>.log` tagged with a failure
   class (`dependency_cache_missing`, `invalid_model_config`,
   `request_too_large`, `gemini_quota_exhausted`, etc.) if all automated
   tiers fail. Failure and integration logs include `Start time:` at the top
   and `End time:` at the bottom.
7. Records per-task attempts, attempted models, duration, and terminal/test
   failure classes in `pipeline-state.json` for crash recovery and observability.
8. Returns to the default branch, moves to next task.

After every task passes, an **integration gate** assembles
`integration/run-<timestamp>` by merging each task branch in dependency
order and runs the full pytest suite against the combined result. On test
failure, the Gemini tier automatically attempts a fix before writing a
failure log. Only a clean integration branch is considered ready for human
merge review.

Re-running is safe — passing branches are skipped, failing branches go straight
to Claude review. Pipeline state persists across crashes.

Before any real run, the referenced `specs/` and `tests/` files must already be
tracked by git and clean. This is required so parallel worktrees and integration
merges see the same inputs as the main working tree.

Pass `--parallel` (or `make parallel`) to run independent tasks concurrently.
The orchestrator partitions the dependency graph into waves and executes each
wave's tasks simultaneously in isolated git worktrees. This trades disk space
and log readability for faster wall-clock time — see `ORCHESTRATION.md` for
the full trade-off discussion.

Model configuration lives in a single `models.yaml` file. Three tiers run in
sequence; adding models to a tier or changing their order is the only config
needed to change routing behaviour.

The generated `Makefile` also writes command transcripts such as
`forgeLogs/run-<timestamp>.log`, `forgeLogs/validate-<timestamp>.log`, and
`forgeLogs/preflight-<timestamp>.log` instead of dropping `*.log` files in the
repo root. Those transcript logs also include explicit start and end times.

| Tier | Models | Trigger |
|------|--------|---------|
| Primary | GPT-OSS 20B | Every task, first |
| Escalation | GPT-OSS 120B → Llama 3.3 70B | Primary exhausted |
| Gemini | Gemini 2.5 Flash | Escalation exhausted and integration repair; requires `GOOGLE_API_KEY` |

---

## Repo structure

```
tiered-llm-forge/
├── new-project.sh           # entry point — creates a new project
├── lib/                     # shell modules sourced by new-project.sh
│   ├── structure.sh         # creates dirs and runs git init
│   ├── dotfiles.sh          # installs .env and .gitignore
│   ├── configs.sh           # installs .aider.conf.yml and models.yaml
│   ├── python.sh            # installs orchestrator package and Makefile
│   └── docs.sh              # installs ORCHESTRATION.md, CLAUDE.md, docs/
├── templates/               # source files copied into every generated project
│   ├── .env.example
│   ├── .gitignore
│   ├── .aider.conf.yml
│   ├── models.yaml          # single source of model configuration
│   ├── requirements.txt     # Python dependencies (pyyaml, pytest, pytest-timeout)
│   ├── Makefile             # convenience recipes (make run, make validate, etc.)
│   ├── ORCHESTRATION.md
│   ├── CLAUDE.md
│   ├── docs/
│   │   ├── SPEC_FORMAT.md
│   │   ├── FAILURE_PLAYBOOK.md
│   │   └── MERGE_CHECKLIST.md
│   ├── orchestrator/        # pipeline package
│   │   ├── __init__.py      # package init + shared SPECS_DIR
│   │   ├── __main__.py      # CLI entry (python3 -m orchestrator)
│   │   ├── config.py        # reads models.yaml
│   │   ├── spec_parser.py   # frontmatter, validation, topo sort
│   │   ├── model_router.py  # model selection, fallback, rate limits
│   │   ├── preflight.py     # config/env validation + runtime warmups
│   │   ├── runner.py        # per-task + full-suite test execution
│   │   ├── git_ops.py       # branch management, stacking, merging
│   │   ├── task_runner.py   # per-task orchestration + model escalation
│   │   ├── integration.py   # integration gate (merge + full suite)
│   │   ├── summary.py       # pipeline run summary + observability
│   │   ├── parallel.py      # concurrent task execution via worktrees + thread pool
│   │   ├── log.py           # logging configuration (console + file)
│   │   ├── failure_class.py # classifies pytest/aider output
│   │   └── state.py         # pipeline-state.json persistence
│   ├── src/__init__.py
│   └── tests/conftest.py
└── tests/                   # tests for the forge's own orchestrator code
    ├── test_spec_parser.py
    ├── test_model_router.py
    ├── test_preflight.py
    ├── test_runner.py
    ├── test_git_ops.py
    ├── test_state.py
    ├── test_failure_class.py
    ├── test_task_runner.py  # resume-point helpers and regression guard
    ├── test_parallel.py     # wave grouping and earliest-wave correctness
    └── test_cwd.py          # cwd parameter support across runner modules
```

To change what gets generated, edit files in `templates/` or add a new
`lib/*.sh` module and wire it into `new-project.sh`.
