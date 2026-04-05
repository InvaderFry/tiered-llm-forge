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
- [Docker](https://docs.docker.com/get-docker/) for running LiteLLM
- A [Groq API key](https://console.groq.com)
- Python 3.10+ with `pytest` installed

---

## Quickstart

```bash
bash ~/projects/tiered-llm-forge/new-project.sh
```

This creates `~/projects/my-project-YYYYMMDD-HHMMSS/` with everything you need
and makes the initial git commit. Then:

```bash
cd ~/projects/my-project-YYYYMMDD-HHMMSS/
```

From here, follow the workflow in that project's `README.md`.

---

## Daily workflow (inside the generated project)

```
1.  Edit .env — add your GROQ_API_KEY (first time only)
2.  docker start litellm
3.  export $(cat .env | grep -v '#' | xargs)
4.  claude
    > "I want to build [feature]. Generate the specs and tests."
5.  Review specs/ manually before running the pipeline
6.  python3 orchestrator.py
7.  For failures: back to claude
    > "Fix task-NNN. Log is at specs/FAILED-task-NNN-name.log."
8.  Repeat 6–7 until all tasks pass
9.  Squash-merge each passing task branch
```

The pipeline (step 6) uses zero Claude tokens — it runs entirely on free Groq
models via Aider. Claude is only used for planning (step 4) and fixing hard
failures (step 7).

---

## How the pipeline works

Each `specs/task-NNN-name.md` file is a task. The orchestrator processes them in
order:

1. Creates a `task/task-NNN-name` git branch
2. Runs Aider with **Qwen3 32B → Kimi K2 → Llama 4 Scout** via LiteLLM (3 attempts)
3. Escalates to **GPT-OSS 120B** if the primary tier fails (2 attempts)
4. Writes `specs/FAILED-task-NNN-name.log` if all attempts fail
5. Returns to the default branch and moves to the next task

Re-running is safe — passing branches are skipped, failing branches go straight
to Claude review.

---

## Repo structure

```
tiered-llm-forge/
├── new-project.sh       # entry point — run this to create a new project
├── lib/                 # shell modules sourced by new-project.sh
│   ├── structure.sh     # creates dirs and runs git init
│   ├── dotfiles.sh      # installs .env and .gitignore
│   ├── configs.sh       # installs .aider.conf.yml and litellm-config.yaml
│   ├── python.sh        # installs orchestrator.py and Python boilerplate
│   └── docs.sh          # installs README.md and CLAUDE.md
└── templates/           # source files copied into every generated project
    ├── .env.example
    ├── .gitignore
    ├── .aider.conf.yml
    ├── litellm-config.yaml
    ├── orchestrator.py
    ├── README.md
    ├── CLAUDE.md
    ├── src/__init__.py
    └── tests/conftest.py
```

To change what gets generated, edit files in `templates/` or add a new
`lib/*.sh` module and wire it into `new-project.sh`.
