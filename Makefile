.PHONY: install test test-py test-ts lint clean start stop

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

install:
	uv sync
	uv pip install -e "./core[dev]" -e "./sdks/python[dev]"
	cd sdks/typescript && npm install

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

test: test-py test-ts

test-py:
	uv run pytest core/tests/ sdks/python/tests/ -v

test-ts:
	cd sdks/typescript && npx tsc --noEmit

# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------

lint:
	uv run ruff check core/src/ core/tests/
	cd sdks/typescript && npx tsc --noEmit

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

status:
	uv run aitelier status

start:
	./scripts/start.sh

stop:
	./scripts/stop.sh

# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------

clean:
	rm -rf .venv runs/ core/src/*.egg-info sdks/python/src/*.egg-info
	rm -rf sdks/typescript/dist sdks/typescript/node_modules
