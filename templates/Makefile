.PHONY: setup env plan run validate dry-run fix review status clean

# Load .env automatically for all targets
ifneq (,$(wildcard .env))
    include .env
    export
endif

setup: ## Install Python dependencies
	pip install -r requirements.txt

env: ## Print loaded environment (confirm keys are set)
	@echo "GROQ_API_KEY=$${GROQ_API_KEY:+set (hidden)}"
	@echo "GOOGLE_API_KEY=$${GOOGLE_API_KEY:+set (hidden)}"

plan: ## Open Claude Code for planning
	claude

validate: ## Validate all specs before running the pipeline
	python3 -m orchestrator --validate

dry-run: ## Preview what the pipeline would do without running models
	python3 -m orchestrator --dry-run

run: ## Run the orchestrator pipeline
	python3 -m orchestrator

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
	@ls specs/FAILED-*.log 2>/dev/null || echo "  (none)"
	@echo ""
	@echo "=== Pipeline state ==="
	@cat pipeline-state.json 2>/dev/null || echo "  (no state file — pipeline hasn't run yet)"

clean: ## Remove failure logs and pipeline state (branches are preserved)
	rm -f specs/FAILED-*.log pipeline-state.json
	@echo "Cleaned failure logs and pipeline state."

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
