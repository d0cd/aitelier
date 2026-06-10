.PHONY: install test test-py test-ts test-live test-host-mode-e2e test-docker-mode-e2e test-brig-mode-e2e test-all-modes-e2e lint clean reset start stop restart logs status doctor

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

install:
	uv sync
	uv pip install -e "./core[dev]" -e "./sdks/python[dev]"
	cd sdks/typescript && pnpm install

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

test: test-py test-ts

test-py:
	# AITELIER_TEST_DATABASE_URL points at the dev Postgres that `make start`
	# boots. The Postgres-integration tests in test_storage.py require it.
	# Override (e.g. for CI) by exporting before `make test`.
	AITELIER_TEST_DATABASE_URL="$${AITELIER_TEST_DATABASE_URL:-postgresql://aitelier:aitelier_local@127.0.0.1:5433/aitelier}" \
	  uv run pytest core/tests/ sdks/python/tests/ sdks/python-mcp/tests/ -v

test-ts:
	cd sdks/typescript && npx tsc --noEmit && npx vitest run

# Build the Sandbox Agent image without starting the container — catches
# Dockerfile breakage (stale Rivet install URL, bad base image, apk drift).
# Skips cleanly if `docker` isn't on PATH.
test-docker-build:
	@if command -v docker >/dev/null 2>&1; then \
		echo "=== Building sandbox-agent image (smoke) ==="; \
		cd docker && docker compose --profile sa build sandbox-agent; \
	else \
		echo "  docker not installed — skipping test-docker-build"; \
	fi

# Full end-to-end against Sandbox Agent running in Docker. Destructive:
# stops your running host-mode SA, swaps the active aitelier config to
# `mode = "docker"`, runs the live suite against the Docker-hosted SA,
# then restores the original config and restarts. Run manually only:
#   make test-docker-mode-e2e
test-docker-mode-e2e:
	@./scripts/test-docker-mode.sh

# Local brig deployment e2e. Skips cleanly if `brig` isn't installed.
# Brig cell hosts only Sandbox Agent (docs/deploy/sandbox-agent.cell.yaml);
# aitelier itself runs on the host as a subprocess pointed at the cell's
# ingress. Tears down on exit.
test-brig-mode-e2e:
	@./scripts/test-brig-mode.sh

# Host-mode e2e: aitelier on host (`make start` infra) + SA on host.
# Assumes `make start` already ran. Run all three deployment paths via
# `make test-all-modes-e2e`.
test-host-mode-e2e:
	@curl -sf http://127.0.0.1:7777/v1/health >/dev/null || { \
		echo "✗ aitelier not running on :7777. Run 'make start' first."; \
		exit 1; \
	}
	@$(MAKE) test-live

# End-to-end tests against a running aitelier — same suite, different
# deployment. `make test-live` is the underlying runner; the three
# mode targets above (host / docker / brig) parameterize it with the
# right AITELIER_LIVE_URL + AITELIER_LIVE_AGENT_BACKENDS for the mode.
test-live:
	AITELIER_LIVE_URL=$${AITELIER_LIVE_URL:-http://localhost:7777} \
	  uv run pytest core/tests/live -v

# Three-mode smoke — host + docker + brig in sequence. Each pulls its
# own deployment up + tears down, so the modes don't interfere.
# Total runtime ~5-10 minutes depending on cold caches.
test-all-modes-e2e:
	@$(MAKE) test-host-mode-e2e
	@$(MAKE) test-docker-mode-e2e
	@$(MAKE) test-brig-mode-e2e

# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------

lint:
	uv run ruff check core/src/ core/tests/
	cd sdks/typescript && npx tsc --noEmit

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

# Boot everything. Idempotent — re-running skips what's already up.
start:
	./scripts/start.sh

# Tear everything down. Postgres data volume survives (use `make reset` to wipe it).
stop:
	./scripts/stop.sh

# Restart just the aitelier service. Leaves infra (Postgres/LiteLLM/SA) alone —
# they're slow to boot, and most edits don't touch them.
restart:
	./scripts/stop.sh service
	./scripts/start.sh service

# Tail the service + sandbox-agent logs (Ctrl-C to exit).
logs:
	@mkdir -p runs/logs
	@touch runs/logs/aitelier.log runs/.sandbox-agent.log
	tail -F runs/logs/aitelier.log runs/.sandbox-agent.log

# What's running, where logs are, are dependencies healthy.
status:
	@./scripts/status.sh

# Preflight: port conflicts, missing credentials, Docker reachability.
# Run this when `make start` fails with a confusing error.
doctor:
	@./scripts/doctor.sh

# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------

# Build artifacts only. Leaves runs/, logs, and Postgres data untouched.
# For a clean slate including data, use `make reset`.
clean:
	rm -rf .venv core/src/*.egg-info sdks/python/src/*.egg-info
	rm -rf sdks/typescript/dist sdks/typescript/node_modules

# Nuclear: stop everything AND drop the Postgres volume + runs/. Confirms first.
reset:
	@printf "This deletes ALL durable state (Postgres volume + runs/). Type 'yes' to continue: "; \
	read confirm; \
	if [ "$$confirm" = "yes" ]; then \
		./scripts/stop.sh; \
		cd docker && docker compose down -v; \
		rm -rf "$(CURDIR)/runs/"; \
		echo "Reset complete. Run 'make start' for a fresh stack."; \
	else \
		echo "Aborted."; \
	fi
