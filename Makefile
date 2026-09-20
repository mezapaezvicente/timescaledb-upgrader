# timescaledb-upgrader
#
# All recipes shell out to scripts/*.sh, which hold the real logic. The Makefile
# is a table of contents, not a program.

# Recipes invoke `bash` explicitly, so this works whether make runs under a
# POSIX shell or cmd.exe, as long as bash is on PATH (Git Bash / WSL on Windows).
ifneq ($(OS),Windows_NT)
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
endif

# Knobs. Override on the command line, e.g. `make up REPLICAS=3`.
KIND_CLUSTER  ?= tsdb-upgrader
CLUSTER_NAME  ?= tsdb
NAMESPACE     ?= timescale
TSDB_IMAGE    ?= timescale/timescaledb-ha:pg16.15-ts2.30.1
REPLICAS      ?= 1
STORAGE       ?= 5Gi
APP_DB        ?= bench

# Seed knobs (see `make seed`).
ROWS           ?= 100000
CHUNK_INTERVAL ?= 1 day
DEVICES        ?= 10

export KIND_CLUSTER CLUSTER_NAME NAMESPACE TSDB_IMAGE REPLICAS STORAGE APP_DB
export ROWS CHUNK_INTERVAL DEVICES

.DEFAULT_GOAL := help
.PHONY: help up down status psql seed smoke logs load-image clean

help: ## Show this help
	@echo "timescaledb-upgrader"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Knobs: REPLICAS, TSDB_IMAGE, ROWS, CHUNK_INTERVAL, DEVICES, NAMESPACE"

up: ## Create the kind cluster and bring up TimescaleDB under Patroni
	@bash scripts/up.sh

down: ## Destroy the kind cluster and everything in it
	@bash scripts/down.sh

status: ## Show Patroni members, pods and services
	@bash scripts/status.sh

psql: ## Open an interactive psql shell on the current primary
	@bash scripts/psql.sh

seed: ## Write time-series data into a hypertable (ROWS, CHUNK_INTERVAL, DEVICES)
	@bash scripts/seed.sh

smoke: ## End-to-end check: connect, create hypertable, insert, query
	@bash scripts/smoke.sh

logs: ## Tail Patroni/PostgreSQL logs from the primary
	@bash scripts/logs.sh

load-image: ## Side-load $(TSDB_IMAGE) from local Docker into the kind node
	@bash scripts/load-image.sh

clean: ## Delete the namespace but keep the kind cluster
	@bash scripts/clean.sh
