# BranchOps Intelligence — developer and operator tasks.  Run `make help` for the list.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON        ?= python3.12
VENV          ?= .venv
BIN           := $(VENV)/bin
LLM_MODE      ?= host
COMPOSE       ?= docker compose -f deploy/docker/compose.yaml $(if $(wildcard .env),--env-file .env)
IMAGE         ?= branchops-intelligence:dev
PROD_IMAGE    ?= registry.bank.internal/branchops/branchops-intelligence:0.1.0
PROD_BASE     ?= registry.bank.internal/ubi9/python-312:latest
POSTGRES_PORT ?= 55432
API_URL       ?= http://localhost:8080
WEBHOOK_SECRET?= dev-secret
WORKERS       ?= 1
TEST_DATABASE_URL ?= postgresql+psycopg://branchops:branchops@localhost:$(POSTGRES_PORT)/branchops_test

export IMAGE

# Model wiring per LLM_MODE: host (Ollama on this machine, GPU/Metal), docker (ollama compose service), mock.
ifeq ($(LLM_MODE),mock)
export LLM_PROVIDER := mock
else ifeq ($(LLM_MODE),docker)
export LLM_PROVIDER := openai-compatible
export LLM_BASE_URL := http://ollama:11434/v1
else
export LLM_PROVIDER := openai-compatible
export LLM_BASE_URL := http://host.docker.internal:11434/v1
endif
OLLAMA_MODEL  ?= branchops-gemma3:4b

##@ Help
.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make \033[36m<target>\033[0m\n"} \
	  /^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 } \
	  /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)

##@ Local development (virtualenv)
$(BIN)/python:
	$(PYTHON) -m venv $(VENV)

.PHONY: install
install: $(BIN)/python ## Create .venv and install the project with dev tools
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e ".[dev]"

.PHONY: lint
lint: ## Ruff lint + format check
	$(BIN)/ruff check src tests migrations scripts
	$(BIN)/ruff format --check src tests migrations scripts

.PHONY: format
format: ## Apply ruff formatting and safe fixes
	$(BIN)/ruff check --fix src tests migrations scripts
	$(BIN)/ruff format src tests migrations scripts

.PHONY: typecheck
typecheck: ## mypy (strict)
	$(BIN)/mypy

.PHONY: test-unit
test-unit: ## Unit tests (no database, no model)
	$(BIN)/pytest tests/unit -q

.PHONY: test
test: db ## All tests incl. PostgreSQL integration (starts compose postgres)
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/pytest -q

.PHONY: check
check: lint typecheck test ## Lint, type check and full test suite

.PHONY: test-llm
test-llm: ## Smoke tests against a real endpoint (needs BRANCHOPS_CONFIG, LLM_* env)
	BRANCHOPS_LLM_INTEGRATION=1 $(BIN)/pytest -m llm_integration tests/integration/test_llm_endpoint.py -q

##@ Docker / docker compose
.PHONY: docker-build
docker-build: ## Build the application image ($(IMAGE)) for local use
	$(COMPOSE) build migrate

.PHONY: model
model: ## Prepare the Gemma model for LLM_MODE (host: ollama create; docker: pull inside compose)
ifeq ($(LLM_MODE),host)
	@command -v ollama >/dev/null || { echo "ollama not found: install Ollama or use LLM_MODE=docker|mock"; exit 1; }
	@ollama list | grep -q '^gemma3:4b' || ollama pull gemma3:4b
	ollama create $(OLLAMA_MODEL) -f deploy/ollama/Modelfile.gemma3
else ifeq ($(LLM_MODE),docker)
	$(COMPOSE) --profile ollama up -d --wait ollama
	$(COMPOSE) --profile ollama run --rm ollama-init
else
	@echo "LLM_MODE=mock: no model needed"
endif

.PHONY: up
up: model ## Start the stack (LLM_MODE=host|docker|mock, WORKERS=n); simulator posts every 5 min
	$(COMPOSE) $(if $(filter docker,$(LLM_MODE)),--profile ollama) up -d --build --wait --scale worker=$(WORKERS) \
	  postgres migrate api worker dashboard simulator $(if $(filter docker,$(LLM_MODE)),ollama)
	@echo ""
	@echo "  Live agents  http://localhost:$${DASHBOARD_PORT:-8090}/live"
	@echo "  Dashboard    http://localhost:$${DASHBOARD_PORT:-8090}"
	@echo "  API          http://localhost:$${API_PORT:-8080}   (health: /health/ready, metrics: /metrics)"
	@echo "  Postgres     localhost:$(POSTGRES_PORT)  (branchops/branchops)"
	@echo "  Model        $(LLM_MODE) · $${LLM_MODEL:-$(OLLAMA_MODEL)} · $(LLM_BASE_URL)"
	@echo "  Simulator    one random incident every $${SIMULATOR_INTERVAL_SECONDS:-300}s (make sim-once for one now)"

.PHONY: sim-once
sim-once: ## Post one random simulated incident right now
	$(COMPOSE) run --rm --no-deps simulator simulate --once --burst 1

.PHONY: sim-stop sim-start
sim-stop: ## Pause the incident simulator
	$(COMPOSE) stop simulator
sim-start: ## Resume the incident simulator
	$(COMPOSE) start simulator

.PHONY: db
db: ## Start only PostgreSQL (for running the app or tests from the virtualenv)
	$(COMPOSE) up -d --wait postgres

.PHONY: down
down: ## Stop the stack (keeps the database volume)
	$(COMPOSE) --profile ollama down

.PHONY: clean-docker
clean-docker: ## Stop the stack and delete the database (and in-docker model) volumes
	$(COMPOSE) --profile ollama down -v --remove-orphans

.PHONY: restart
restart: ## Restart api, worker and dashboard
	$(COMPOSE) restart api worker dashboard

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: logs
logs: ## Follow logs (SERVICE=api|worker|dashboard|simulator|migrate|postgres to filter)
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

.PHONY: migrate
migrate: ## Run migrations + template publication in the stack
	$(COMPOSE) run --rm migrate

.PHONY: seed
seed: ## Send signed sample Jira webhooks to the API
	$(PYTHON) scripts/send_samples.py --url $(API_URL) --secret $(WEBHOOK_SECRET)

.PHONY: health
health: ## Probe api, worker and dashboard health endpoints
	@curl -fsS $(API_URL)/health/ready && echo "  <- api"
	@$(COMPOSE) exec -T worker python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8081/readyz').read().decode())" && echo "  <- worker"
	@curl -fsS http://localhost:$${DASHBOARD_PORT:-8090}/health/ready && echo "  <- dashboard"

.PHONY: overview
overview: ## Print the analytics overview from the API
	@curl -fsS $(API_URL)/api/analytics/overview | $(PYTHON) -m json.tool

.PHONY: shell
shell: ## Open a shell in the api container
	$(COMPOSE) exec api /bin/sh

.PHONY: psql
psql: ## Open psql in the postgres container
	$(COMPOSE) exec postgres psql -U branchops -d branchops

.PHONY: llm-health
llm-health: ## Check the configured model endpoint from inside the stack
	$(COMPOSE) exec worker branchops llm health

##@ Run from the virtualenv (against `make db`)
.PHONY: run-api run-worker run-dashboard run-mcp
run-api: ## branchops api with config/local.yaml
	BRANCHOPS_CONFIG=config/local.yaml JIRA_WEBHOOK_SECRETS=$(WEBHOOK_SECRET) $(BIN)/branchops api
run-worker: ## branchops worker with config/local.yaml
	BRANCHOPS_CONFIG=config/local.yaml $(BIN)/branchops worker
run-dashboard: ## branchops dashboard with config/local.yaml
	BRANCHOPS_CONFIG=config/local.yaml $(BIN)/branchops dashboard
run-mcp: ## branchops mcp (read-only MCP server on 127.0.0.1:8095/mcp) with config/local.yaml
	BRANCHOPS_CONFIG=config/local.yaml $(BIN)/branchops mcp

##@ Release (air-gapped)
.PHONY: lock
lock: ## Regenerate requirements*.lock (needs uv and index access)
	uv pip compile pyproject.toml --python-version 3.12 -o requirements.lock
	uv pip compile pyproject.toml --extra dev --python-version 3.12 -o requirements-dev.lock

.PHONY: wheelhouse
wheelhouse: ## Build ./wheelhouse from the internal mirror (PIP_INDEX_URL required)
	./scripts/build_wheelhouse.sh

.PHONY: image
image: ## Offline production image build from ./wheelhouse (PROD_BASE, PROD_IMAGE)
	docker build -f deploy/docker/Dockerfile --build-arg BASE_IMAGE=$(PROD_BASE) \
	  --build-arg WHEELHOUSE_SOURCE=offline -t $(PROD_IMAGE) .

.PHONY: k8s-render
k8s-render: ## Render the OpenShift production overlay
	kubectl kustomize deploy/openshift/overlays/production

##@ Housekeeping
.PHONY: clean
clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .nicegui build dist wheelhouse
	find . -name __pycache__ -type d -prune -not -path './$(VENV)/*' -exec rm -rf {} +
