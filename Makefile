# hammerhead-bench Makefile.
#
# One-line upgrade path: edit versions.lock; Makefile picks up via shell-out.
# Everything runs via `uv run` so the pinned dependency set is authoritative.

UV ?= uv
PYTHON ?= $(UV) run python

# Pull image pins from versions.lock so this file never drifts from the lockfile.
FRR_IMAGE     := $(shell grep -E '^FRR_IMAGE='     versions.lock | cut -d= -f2-)
BATFISH_IMAGE := $(shell grep -E '^BATFISH_IMAGE=' versions.lock | cut -d= -f2-)

.PHONY: help install preflight smoke bench bench-fast report clean test lint fmt

help:
	@echo "hammerhead-bench"
	@echo ""
	@echo "  make install     uv sync (install all deps)"
	@echo "  make preflight   verify docker / clab / images / RAM"
	@echo "  make smoke       one-topology end-to-end sanity (phase 2)"
	@echo "  make bench       full run; respects MAX_NODES env var"
	@echo "  make bench-fast  skip topologies > 10 nodes and skip cEOS"
	@echo "  make report      regenerate HTML/MD from results/ without re-running"
	@echo "  make clean       docker cleanup + rm results/ + python caches"
	@echo "  make test        unit tests"
	@echo ""
	@echo "  Pinned images (edit versions.lock to change):"
	@echo "    FRR_IMAGE=$(FRR_IMAGE)"
	@echo "    BATFISH_IMAGE=$(BATFISH_IMAGE)"

install:
	$(UV) sync --extra dev

preflight: install
	$(PYTHON) scripts/preflight.py

smoke:
	@echo "[smoke] phase 2 deliverable. See README.md 'Development order'."
	@exit 1

bench:
	@echo "[bench] phase 7+ deliverable. See README.md 'Development order'."
	@exit 1

bench-fast:
	@echo "[bench-fast] phase 7+ deliverable. See README.md 'Development order'."
	@exit 1

report:
	@echo "[report] phase 9 deliverable. See README.md 'Development order'."
	@exit 1

clean:
	@echo "[clean] destroying dangling clab containers/networks (if any)..."
	-@docker ps -a --filter "label=clab-topo" --format "{{.ID}}" | xargs -r docker rm -f 2>/dev/null || true
	-@docker network ls --filter "label=clab-topo" --format "{{.ID}}" | xargs -r docker network rm 2>/dev/null || true
	@rm -rf results/*.json results/*.html results/*.md results/*.jsonl results/snapshots/
	@find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .ruff_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "[clean] done"

test: install
	$(UV) run pytest tests/

lint: install
	$(UV) run ruff check harness scripts tests

fmt: install
	$(UV) run ruff format harness scripts tests
