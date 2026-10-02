# Nour developer entry points. `make check` is what CI runs (see .github/workflows/ci.yml).
# Requires uv (https://docs.astral.sh/uv/) and, for the compose targets, Docker.

UV      ?= uv
COMPOSE ?= docker compose
PG_URL  ?= postgresql+psycopg://nour:nour@localhost:5432/nour

.DEFAULT_GOAL := help
.PHONY: help sync lint fmt typecheck test test-pg scenarios dryrun up down backup restore-test check

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

sync: ## Install all dependency groups plus the postgres extra into .venv
	$(UV) sync --all-groups --extra postgres

lint: ## Ruff lint and format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt: ## Auto-format and auto-fix with ruff
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## mypy over the nour package (config in pyproject.toml)
	$(UV) run mypy

test: ## Unit tests on SQLite
	$(UV) run pytest -q

test-pg: ## Tests marked `postgres` against a running PostgreSQL (make up, or PG_URL=...)
	NOUR_DATABASE_URL=$(PG_URL) $(UV) run pytest -q -m postgres

scenarios: ## Weekly behavioural regression scenarios (SPEC §13, §16)
	@if [ -d tests/scenarios ]; then $(UV) run pytest -q tests/scenarios; else echo "tests/scenarios does not exist yet"; fi

dryrun: ## 48-hour dry run: drafts and logs, no sends, no spend (SPEC §8, phase 0 gate in §16)
	NOUR_DRY_RUN=true $(UV) run nour dryrun --hours 48

up: ## Start postgres + brain + auditor (add COMPOSE_PROFILES=s3 for MinIO)
	$(COMPOSE) up -d --build

down: ## Stop the stack (volumes are kept; `docker compose down -v` wipes them)
	$(COMPOSE) down

backup: ## Daily encrypted backup of memory, vault, ledger, CRM and audit log (SPEC §14)
	$(UV) run nour backup

# SPEC §14 "Backups and portability": daily encrypted backups to a second UAE-region
# location and a MONTHLY RESTORE TEST. Run restore-test on the first working day of
# each month and record the result in the quarterly review.
restore-test: ## Restore the latest backup into a scratch target and verify it (SPEC §14: monthly restore test)
	$(UV) run nour restore --verify

check: lint typecheck test ## Everything CI runs on a push: lint + typecheck + test
