# RPG Index Study — run harness.
# Typical first run:  make setup up all
# Re-run the experiment only:  make bench report

DC ?= docker compose
PY ?= .venv/bin/python
ROWS ?= 100000
export DATABASE_URL ?= postgresql://rpg:rpg@localhost:5544/rpg

.PHONY: help setup up down wait etl load inflate bench bench-scale bench-write report all clean nuke psql

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## Create the Python venv and install dependencies
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt

up: ## Start PostgreSQL in Docker and wait until ready
	$(DC) up -d
	@$(MAKE) wait

wait:
	@echo "waiting for postgres..."; \
	until $(DC) exec -T db pg_isready -U rpg -d rpg >/dev/null 2>&1; do sleep 1; done; \
	echo "postgres ready."

down: ## Stop the database container (keeps data volume)
	$(DC) down

etl: ## Parse 5etools spell JSON -> normalized CSVs in data/
	$(PY) src/etl.py

load: ## Create schema and load the base (canonical) spells
	$(PY) src/load.py

inflate: ## Synthetically grow to ROWS spells (default 100000)
	$(PY) src/inflate.py --rows $(ROWS)

bench: ## Run the read workload across all treatments + throughput + index-cost
	$(PY) src/bench.py

bench-scale: ## Scaling study: latency vs table size (re-inflates; SIZES overrides)
	$(PY) src/bench_scale.py $(if $(SIZES),--sizes $(SIZES),)

bench-write: ## Real-time study: write tax + concurrency + stale statistics
	$(PY) src/bench_write.py

report: ## (Re)build charts from the latest results CSVs
	$(PY) src/bench.py --report-only

all: etl load inflate bench ## Full pipeline: etl -> load -> inflate -> bench

psql: ## Open a psql shell against the running database
	$(DC) exec db psql -U rpg -d rpg

clean: ## Remove generated CSVs and results
	rm -f data/*.csv results/*.csv results/*.png results/summary.md

nuke: ## Stop the DB and delete its data volume (full reset)
	$(DC) down -v
