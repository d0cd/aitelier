.PHONY: install test test-py test-ts lint clean reset start stop restart logs status doctor

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
	uv run pytest core/tests/ sdks/python/tests/ -v

test-ts:
	cd sdks/typescript && npx tsc --noEmit && npx vitest run

# End-to-end tests against a running aitelier. Boot the stack first
# (`make start`), then `make test-live`. Auto-skipped without env var.
test-live:
	AITELIER_LIVE_URL=$${AITELIER_LIVE_URL:-http://localhost:7777} \
	  uv run pytest core/tests/live -v

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
