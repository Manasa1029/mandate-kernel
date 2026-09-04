.DEFAULT_GOAL := help
PY  ?= python
PIP ?= $(PY) -m pip
DB  ?= kernel.db
PORT ?= 8000

.PHONY: help install install-llm install-dev test test-v cov api seller console demo \
        redteam injection eval verify clean fresh docker-build docker-up lint judge

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-16s\033[0m %s\n",$$1,$$2}'

# ── setup ────────────────────────────────────────────────────────────────────

install:  ## Install runtime dependencies
	$(PIP) install -r requirements.txt

install-dev:  ## Install runtime + test dependencies
	$(PIP) install -r requirements-dev.txt

install-llm:  ## Add the optional LLM planner providers
	$(PIP) install "langchain-anthropic>=0.3" "langchain-openai>=0.3"

# ── verification ─────────────────────────────────────────────────────────────

test:  ## Run the full test suite
	$(PY) -m pytest tests/ -q

test-v:  ## Run the suite with test names
	$(PY) -m pytest tests/ -v

redteam:  ## Run the adversarial corpus and write docs/EVALUATION.md
	$(PY) -m redteam.runner

injection:  ## Run the prompt-injection evaluation
	$(PY) -m redteam.injection

eval: test redteam injection  ## Everything a judge should be able to reproduce
	@echo ""
	@echo "  All three suites green. docs/EVALUATION.md is regenerated."

judge: eval demo  ## One command for a reviewer with 90 seconds
	@echo ""
	@echo "  Now: make api   (then open console/index.html)"

verify:  ## Verify the hash chain of an existing ledger file
	@KERNEL_DB_PATH=$(DB) $(PY) -c "from kernel.store import Store; import os; \
	  ok,bad,msg = Store(os.environ['KERNEL_DB_PATH']).verify_chain(); \
	  print(('INTACT' if ok else f'BROKEN at seq {bad}'), '—', msg); \
	  raise SystemExit(0 if ok else 1)"

# ── running ──────────────────────────────────────────────────────────────────

api:  ## Run the kernel HTTP service (reload on change)
	KERNEL_DB_PATH=$(DB) $(PY) -m uvicorn kernel.api:app --reload --port $(PORT)

seller:  ## Run the seller storefront API on 8100
	$(PY) -m uvicorn seller.app:app --reload --port 8100

mcp:  ## Run the seller MCP server over stdio
	$(PY) -m seller.mcp_server

console:  ## Serve the audit console on 8080
	$(PY) -m http.server 8080 --directory console

demo:  ## Run all six demo scenes in the terminal
	$(PY) -m scripts.demo --db $(DB)

demo-mem:  ## Run the demo without touching the ledger file
	$(PY) -m scripts.demo

# ── housekeeping ─────────────────────────────────────────────────────────────

clean:  ## Remove caches
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache

fresh: clean  ## Remove caches AND the ledger (destructive)
	rm -f $(DB) $(DB)-wal $(DB)-shm

docker-build:  ## Build the container image
	docker build -t mandate-kernel:latest .

docker-up:  ## Run kernel + seller + console via compose
	docker compose up --build
