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
export TARGET_IMAGE

.DEFAULT_GOAL := help
.PHONY: help up down status psql seed smoke logs load-image clean
.PHONY: test release-check matrix matrix-scaffold images images-refresh image-contents lookup preflight drift

# Lookup arguments for `make lookup`.
TARGET_IMAGE ?=
FROM_PG ?=
FROM_TS ?=
TO_PG   ?=
TO_TS   ?=

# Arguments for `make drift`. VIA may list several images.
IMAGE     ?=
INSTALLED ?=
PG_MAJOR  ?=
VIA       ?=

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

# --- version matrix (phase 2) -------------------------------------------------

test: ## Run the unit test suite
	@bash scripts/test.sh

# On a development branch a stale evidence record only warns. Here it
# fails, as it does on a checkout of a tagged release. Run before tagging.
release-check: export TSUPGRADE_RELEASE := 1
release-check: ## Run the suite as a release: a stale evidence record fails
	@bash scripts/test.sh

matrix: ## Render the version matrix to docs/matrix.md
	@bash scripts/tsupgrade.sh matrix render

matrix-scaffold: ## Add transitions the image catalog implies, keeping existing rows
	@bash scripts/tsupgrade.sh matrix scaffold

images: ## Summarise which TimescaleDB HA images exist
	@bash scripts/tsupgrade.sh images show

images-refresh: ## Re-query the registry and regenerate data/images.json
	@bash scripts/tsupgrade.sh images refresh

# Measures what each image can really install (ls in a throwaway container; no
# server). The labels overstate floors and cannot express holes, so measured
# entries overrule them in `drift` and `plan`. The images must be pulled first.
IMAGE_CONTENTS_TAGS ?= pg11.11-ts2.1.1-p0 pg12.6-ts2.0.1-p0 pg12.6-ts2.1.1-p2 pg12.22-ts2.11.2   pg13.22-ts2.15.3 pg14.19-ts2.19.3 pg15.13-ts2.20.3 pg15.18-ts2.28.3   pg16.15-ts2.30.1 pg17.11-ts2.30.1 pg18.6-ts2.30.1   pg12.22-ts2.11.2-all pg13.12-ts2.11.2-all pg14.19-ts2.19.3-all pg15.13-ts2.19.3-all   pg15.18-ts2.28.3-all pg16.15-ts2.30.1-all pg17.11-ts2.30.1-all pg18.6-ts2.30.1-all

image-contents: ## Measure what local images install into data/image-contents.json [IMAGE_CONTENTS_TAGS=...]
	@bash scripts/tsupgrade.sh contents measure $(IMAGE_CONTENTS_TAGS)

lookup: ## Look up one transition: make lookup FROM_PG=16.15 FROM_TS=2.28.3 TO_PG=17 TO_TS=2.30
	@bash scripts/tsupgrade.sh matrix lookup 	  --from-pg "$(FROM_PG)" --from-ts "$(FROM_TS)" 	  --to-pg "$(TO_PG)" --to-ts "$(TO_TS)"

# --- preflight (phase 3) ------------------------------------------------------

preflight: ## Read-only pre-upgrade checks: make preflight TARGET_IMAGE=<image>
	@bash scripts/preflight.sh

# --- drift (phase 4) ----------------------------------------------------------

# Offline: asks whether rolling out an image would strand a database, using
# only registry labels. Worth running *before* the rollout, because afterwards
# the answer is an incident.
drift: ## Would an image strand a version? make drift IMAGE=<ref> INSTALLED=<ver> [PG_MAJOR=15] [VIA=<ref>]
	@bash scripts/tsupgrade.sh drift --image "$(IMAGE)" --installed "$(INSTALLED)" 	  $(if $(PG_MAJOR),--pg-major "$(PG_MAJOR)",) $(foreach v,$(VIA),--via "$(v)")
