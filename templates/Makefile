.PHONY: setup env preflight plan run resume parallel validate dry-run fix review status clean

SHELL := /bin/bash
LOG_DIR := forgeLogs
SYSTEM_PYTHON := python3
LOG_FILE_STAMP = $$( $(SYSTEM_PYTHON) -c 'from datetime import datetime; print(datetime.now().strftime("%Y%m%d-%H%M%S%f"))' )
LOG_TIME_STAMP = $$( $(SYSTEM_PYTHON) -c 'from datetime import datetime; print(datetime.now().astimezone().isoformat(timespec="seconds"))' )

# Load .env automatically for all targets
ifneq (,$(wildcard .env))
    include .env
    export
endif

PYTHON := .venv/bin/python3
PIP    := .venv/bin/pip

setup: ## Create .venv and install Python dependencies
	python3 -m venv .venv
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -r requirements.txt

env: ## Print loaded environment (confirm keys are set)
	@echo "GROQ_API_KEY=$${GROQ_API_KEY:+set (hidden)}"
	@echo "GOOGLE_API_KEY=$${GOOGLE_API_KEY:+set (hidden)}"
	@echo "GEMINI_API_KEY=$${GEMINI_API_KEY:+set (hidden, legacy alias)}"

preflight: ## Validate config, provider env, and runtime prerequisites
	@mkdir -p $(LOG_DIR)
	@log_file="$(LOG_DIR)/preflight-$(LOG_FILE_STAMP).log"; \
	started_at="$(LOG_TIME_STAMP)"; \
	set -o pipefail; \
	{ \
		printf 'Start time: %s\n\n' "$$started_at"; \
		status=0; \
		$(PYTHON) -m orchestrator --preflight 2>&1 || status=$$?; \
		printf '\nEnd time: %s\n' "$(LOG_TIME_STAMP)"; \
		exit $$status; \
	} | tee "$$log_file"

plan: ## Open Claude Code for planning
	claude

validate: ## Validate all specs before running the pipeline
	@mkdir -p $(LOG_DIR)
	@log_file="$(LOG_DIR)/validate-$(LOG_FILE_STAMP).log"; \
	started_at="$(LOG_TIME_STAMP)"; \
	set -o pipefail; \
	{ \
		printf 'Start time: %s\n\n' "$$started_at"; \
		status=0; \
		$(PYTHON) -m orchestrator --validate 2>&1 || status=$$?; \
		printf '\nEnd time: %s\n' "$(LOG_TIME_STAMP)"; \
		exit $$status; \
	} | tee "$$log_file"

dry-run: ## Preview what the pipeline would do without running models
	@mkdir -p $(LOG_DIR)
	@log_file="$(LOG_DIR)/dry-run-$(LOG_FILE_STAMP).log"; \
	started_at="$(LOG_TIME_STAMP)"; \
	set -o pipefail; \
	{ \
		printf 'Start time: %s\n\n' "$$started_at"; \
		status=0; \
		$(PYTHON) -m orchestrator --dry-run 2>&1 || status=$$?; \
		printf '\nEnd time: %s\n' "$(LOG_TIME_STAMP)"; \
		exit $$status; \
	} | tee "$$log_file"

run: ## Run the orchestrator pipeline
	@mkdir -p $(LOG_DIR)
	@log_file="$(LOG_DIR)/run-$(LOG_FILE_STAMP).log"; \
	started_at="$(LOG_TIME_STAMP)"; \
	set -o pipefail; \
	{ \
		printf 'Start time: %s\n\n' "$$started_at"; \
		status=0; \
		$(PYTHON) -m orchestrator 2>&1 || status=$$?; \
		printf '\nEnd time: %s\n' "$(LOG_TIME_STAMP)"; \
		exit $$status; \
	} | tee "$$log_file"

resume: ## Resume a crashed/failed run from where it left off
	@mkdir -p $(LOG_DIR)
	@log_file="$(LOG_DIR)/resume-$(LOG_FILE_STAMP).log"; \
	started_at="$(LOG_TIME_STAMP)"; \
	set -o pipefail; \
	{ \
		printf 'Start time: %s\n\n' "$$started_at"; \
		status=0; \
		$(PYTHON) -m orchestrator --resume 2>&1 || status=$$?; \
		printf '\nEnd time: %s\n' "$(LOG_TIME_STAMP)"; \
		exit $$status; \
	} | tee "$$log_file"

parallel: ## Run independent tasks concurrently in dependency waves (default 4 workers)
	@mkdir -p $(LOG_DIR)
	@log_file="$(LOG_DIR)/parallel-$(LOG_FILE_STAMP).log"; \
	started_at="$(LOG_TIME_STAMP)"; \
	set -o pipefail; \
	{ \
		printf 'Start time: %s\n\n' "$$started_at"; \
		status=0; \
		$(PYTHON) -m orchestrator --parallel 2>&1 || status=$$?; \
		printf '\nEnd time: %s\n' "$(LOG_TIME_STAMP)"; \
		exit $$status; \
	} | tee "$$log_file"

fix: ## Open Claude Code to fix failures
	@echo "Opening Claude Code — say 'Fix the failures.'"
	claude

review: ## Open Claude Code to review and merge branches
	@echo "Opening Claude Code — say 'Review the task branches and merge them.'"
	claude

status: ## Show task branch status and failure logs
	@echo "=== Task branches ==="
	@git branch --list "task/*" 2>/dev/null || echo "  (none)"
	@echo ""
	@echo "=== Failure logs ==="
	@ls forgeLogs/FAILED-*.log 2>/dev/null || echo "  (none)"
	@echo ""
	@echo "=== Pipeline state ==="
	@cat pipeline-state.json 2>/dev/null || echo "  (no state file — pipeline hasn't run yet)"

clean: ## Remove forgeLogs directory, pipeline state, and legacy root logs (branches are preserved)
	rm -rf forgeLogs pipeline-state.json orchestrator.log preflight.log validate.log dry-run.log run.log
	@echo "Cleaned forgeLogs, pipeline state, and legacy root logs."

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
