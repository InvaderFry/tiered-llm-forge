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
- Python 3.10+ with `pip`

---

## Quickstart

```bash
bash ~/projects/tiered-llm-forge/new-project.sh
```

This creates `~/projects/my-project-YYYYMMDD-HHMMSS/` with everything you need.
Then:

```bash
cd ~/projects/my-project-YYYYMMDD-HHMMSS/
pip install -r requirements.txt
```

From here, follow the workflow in that project's `ORCHESTRATION.md`.

---

## Daily workflow (inside the generated project)

```
1.  Edit .env — add your GROQ_API_KEY (first time only)
2.  export $(cat .env | grep -v '#' | xargs)
3.  claude
    > "I want to build [feature]. Generate the specs and tests."
4.  make validate                    # check specs for errors
5.  Review specs/ manually
6.  make run                         # run the pipeline
7.  For failures: back to claude
    > "Fix the failures."
8.  Repeat 6-7 until all tasks pass
9.  Back to claude for the final quality gate
    > "Review the task branches and merge them."
```

The pipeline (step 6) uses zero Claude tokens — it runs on free Groq models
via Aider. Claude is only used for planning (step 3) and fixing hard failures
(step 7).

---

## How the pipeline works

Each `specs/task-NNN-name.md` file is a task with YAML frontmatter that declares
the target file, test file, and dependencies. The orchestrator processes them in
dependency order:

1. Creates a `task/task-NNN-name` git branch
2. Tries primary tier models with automatic fallback (3 attempts)
3. Escalates to the escalation tier if primary fails (2 attempts)
4. Writes `specs/FAILED-task-NNN-name.log` if all attempts fail
5. Records state in `pipeline-state.json` for crash recovery
6. Returns to default branch, moves to next task

Re-running is safe — passing branches are skipped, failing branches go straight
to Claude review. Pipeline state persists across crashes.

Model configuration lives in a single `models.yaml` file. Add new providers
(Google AI Studio, etc.) by adding model names to the appropriate tier.

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
│   ├── requirements.txt     # Python dependencies (pyyaml, pytest)
│   ├── Makefile             # convenience recipes (make run, make validate, etc.)
│   ├── ORCHESTRATION.md
│   ├── CLAUDE.md
│   ├── docs/
│   │   ├── SPEC_FORMAT.md
│   │   ├── FAILURE_PLAYBOOK.md
│   │   └── MERGE_CHECKLIST.md
│   ├── orchestrator/        # pipeline package
│   │   ├── __init__.py
│   │   ├── __main__.py      # CLI entry (python3 -m orchestrator)
│   │   ├── config.py        # reads models.yaml
│   │   ├── spec_parser.py   # frontmatter, compression, validation, topo sort
│   │   ├── model_router.py  # model selection, fallback, rate limits
│   │   ├── runner.py        # test execution, regression detection
│   │   ├── git_ops.py       # branch management
│   │   └── state.py         # pipeline-state.json persistence
│   ├── src/__init__.py
│   └── tests/conftest.py
└── tests/                   # tests for the forge's own orchestrator code
    ├── test_spec_parser.py
    ├── test_model_router.py
    └── test_state.py
```

To change what gets generated, edit files in `templates/` or add a new
`lib/*.sh` module and wire it into `new-project.sh`.
